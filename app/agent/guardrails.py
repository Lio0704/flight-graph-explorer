"""Guardrails around the LLM: input checks, Cypher validation, output grounding.

Defence in depth, from cheapest to strongest:

1. Input guard      - length limits, prompt-injection patterns.
2. Static Cypher guard (this module, no DB call):
     single statement, read-only clauses only, no procedures (CALL x.y / apoc / dbms),
     must start with a read clause and RETURN something, labels / relationship
     types / properties must exist in the schema (catches hallucinated schema),
     variable-length traversals must be bounded, LIMIT enforced / capped.
3. EXPLAIN          - Neo4j compiles the query without running it (syntax + semantics).
4. Read transaction - execution happens in a READ transaction with a timeout, so even
                      a write that slipped through is refused by Neo4j itself.
5. Grounding check  - codes and numbers quoted in the LLM's final answer must appear
                      in the rows returned by Neo4j; otherwise the answer is flagged.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from app.queries.schema import ALLOWED_PROPERTIES, NODE_LABELS, RELATIONSHIP_TYPES

MAX_QUESTION_CHARS = 500
MAX_VAR_LENGTH_HOPS = 5

INJECTION_PATTERNS = [
    r"ignore (all |any )?(the |your )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |your )?(previous|prior|above|system)",
    r"(reveal|print|show) (me )?(the |your )?system prompt",
    r"you are now",
    r"developer mode",
    r"jailbreak",
]

WRITE_KEYWORDS = ["CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH", "LOAD", "ALTER",
                  "GRANT", "DENY", "REVOKE", "START", "STOP", "TERMINATE", "USE", "SHOW", "FINISH"]
READ_START = re.compile(r"^\s*(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND|RETURN|CALL\s*[({])", re.IGNORECASE)
PROCEDURE_CALL = re.compile(r"\bCALL\s+[A-Za-z_]", re.IGNORECASE)
NODE_LABEL = re.compile(r"\(\s*[A-Za-z_]?\w*\s*:\s*([A-Za-z_][\w:&|!]*)")
REL_TYPE = re.compile(r"\[\s*[A-Za-z_]?\w*\s*:\s*([A-Za-z_][\w:&|!]*)")
LABEL_PREDICATE = re.compile(r"\b[a-z_]\w*\s*:\s*([A-Z]\w*)\b")
PROPERTY_ACCESS = re.compile(r"\b[A-Za-z_]\w*\.([A-Za-z_]\w*)\b(?!\s*\()")
VAR_LENGTH = re.compile(r"\[\s*\w*\s*(?::\s*[\w|:&!]+\s*)?\*\s*(\d*)\s*(\.\.)?\s*(\d*)")
BOUND_NODE_VAR = re.compile(r"(?<![\w.])\(\s*([A-Za-z_]\w*)\s*[:){]")
BOUND_REL_VAR = re.compile(r"\[\s*([A-Za-z_]\w*)\s*[:*\]{]")
QUANTIFIER = re.compile(r"\)\s*\{\s*(\d*)\s*,\s*(\d*)\s*\}")
FINAL_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)
STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
FUNCTION_NAMESPACES = {"point", "date", "datetime", "duration", "localdatetime", "localtime", "time", "gds", "apoc"}

GROUNDING_STOPWORDS = {"THE", "AND", "NOT", "FOR", "ARE", "YES", "API", "ONE", "TWO", "BUT", "HAS", "ALL", "ANY"}


@dataclass
class GuardrailEvent:
    """One check, recorded for transparency in the API response."""

    stage: str
    check: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        """JSON-friendly representation."""
        return asdict(self)


class GuardrailViolation(Exception):
    """Raised when a check fails; carries the list of events."""

    def __init__(self, events: list[GuardrailEvent]) -> None:
        self.events = events
        failed = [e for e in events if not e.passed]
        super().__init__("; ".join(f"{e.check}: {e.detail}" for e in failed))


# --------------------------------------------------------------------------- #
# 1. Input guard
# --------------------------------------------------------------------------- #
def check_question(question: str) -> list[GuardrailEvent]:
    """Validate the user's question before any LLM call."""
    events = [
        GuardrailEvent("input", "not_empty", bool(question.strip()), "" if question.strip() else "question is empty"),
        GuardrailEvent(
            "input", "max_length", len(question) <= MAX_QUESTION_CHARS,
            f"{len(question)} chars (max {MAX_QUESTION_CHARS})",
        ),
    ]
    hits = [p for p in INJECTION_PATTERNS if re.search(p, question, re.IGNORECASE)]
    events.append(GuardrailEvent("input", "prompt_injection", not hits, f"matched: {hits}" if hits else ""))
    return events


# --------------------------------------------------------------------------- #
# 2. Static Cypher guard
# --------------------------------------------------------------------------- #
def strip_literals(query: str) -> str:
    """Remove comments and string literals so keywords inside strings are ignored."""
    return STRING_LITERAL.sub("''", COMMENT.sub(" ", query))


def normalise_cypher(query: str) -> str:
    """Remove markdown fences, surrounding whitespace and a trailing semicolon."""
    query = query.strip()
    query = re.sub(r"^```(?:cypher)?\s*|\s*```$", "", query, flags=re.IGNORECASE).strip()
    return query[:-1].rstrip() if query.endswith(";") else query


def _split_names(group: str) -> list[str]:
    return [name for name in re.split(r"[:&|!]", group) if name]


def _schema_events(code: str) -> list[GuardrailEvent]:
    labels = {n for m in NODE_LABEL.findall(code) for n in _split_names(m)}
    labels |= set(LABEL_PREDICATE.findall(code)) - set(RELATIONSHIP_TYPES)
    rels = {n for m in REL_TYPE.findall(code) for n in _split_names(m)}
    # Only check `var.prop` when `var` is bound to a node or relationship in a pattern;
    # `d.country` on a map built with collect({country: ...}) is legitimate.
    graph_vars = set(BOUND_NODE_VAR.findall(code)) | set(BOUND_REL_VAR.findall(code))
    props = {
        m.group(1) for m in PROPERTY_ACCESS.finditer(code)
        if m.group(0).split(".")[0] in graph_vars and m.group(0).split(".")[0].lower() not in FUNCTION_NAMESPACES
    }
    unknown_labels = sorted(labels - set(NODE_LABELS))
    unknown_rels = sorted(rels - set(RELATIONSHIP_TYPES))
    unknown_props = sorted(props - set(ALLOWED_PROPERTIES))
    return [
        GuardrailEvent("cypher", "schema_labels", not unknown_labels,
                       f"unknown labels {unknown_labels}; valid: {sorted(NODE_LABELS)}" if unknown_labels else ""),
        GuardrailEvent("cypher", "schema_relationships", not unknown_rels,
                       f"unknown types {unknown_rels}; valid: {sorted(RELATIONSHIP_TYPES)}" if unknown_rels else ""),
        GuardrailEvent("cypher", "schema_properties", not unknown_props,
                       f"unknown properties {unknown_props}" if unknown_props else ""),
    ]


def _bounded_traversal(code: str) -> GuardrailEvent:
    problems = []
    for match in VAR_LENGTH.finditer(code):
        lower, dots, upper = match.groups()
        if dots and not upper:
            problems.append(match.group(0).strip() + "] (no upper bound)")
        elif not dots and not lower:
            problems.append("* (unbounded)")
        elif int(upper or lower) > MAX_VAR_LENGTH_HOPS:
            problems.append(match.group(0).strip() + f" (> {MAX_VAR_LENGTH_HOPS} hops)")
    for match in QUANTIFIER.finditer(code):
        upper = match.group(2)
        if not upper or int(upper) > MAX_VAR_LENGTH_HOPS:
            problems.append(match.group(0) + " (unbounded or too deep)")
    return GuardrailEvent("cypher", "bounded_traversal", not problems, "; ".join(problems))


def _procedure_event(code: str) -> GuardrailEvent:
    calls = PROCEDURE_CALL.search(code) or re.search(r"\b(apoc|dbms|db)\.", code, re.IGNORECASE)
    return GuardrailEvent("cypher", "no_procedures", not calls, "procedure calls are not allowed" if calls else "")


class CypherGuard:
    """Static validator / rewriter for LLM-generated Cypher."""

    def __init__(self, max_rows: int = 50) -> None:
        self.max_rows = max_rows

    def check(self, raw_query: str) -> tuple[str, list[GuardrailEvent]]:
        """Return (safe query, events). Raise GuardrailViolation if any check fails."""
        query = normalise_cypher(raw_query)
        code = strip_literals(query)
        upper = code.upper()
        writes = sorted({kw for kw in WRITE_KEYWORDS if re.search(rf"\b{kw}\b", upper)})
        events = [
            GuardrailEvent("cypher", "single_statement", ";" not in code, "multiple statements" if ";" in code else ""),
            GuardrailEvent("cypher", "read_only", not writes, f"forbidden clauses: {writes}" if writes else ""),
            _procedure_event(code),
            GuardrailEvent("cypher", "starts_with_read_clause", bool(READ_START.match(code)), code[:30]),
            GuardrailEvent("cypher", "has_return", bool(re.search(r"\bRETURN\b", upper)), ""),
            *_schema_events(code),
            _bounded_traversal(code),
        ]
        if not all(e.passed for e in events):
            raise GuardrailViolation(events)
        query, limit_event = self.enforce_limit(query)
        events.append(limit_event)
        return query, events

    def enforce_limit(self, query: str) -> tuple[str, GuardrailEvent]:
        """Append LIMIT when missing; lower it when above max_rows."""
        match = FINAL_LIMIT.search(query)
        if not match:
            return f"{query}\nLIMIT {self.max_rows}", GuardrailEvent(
                "cypher", "limit_enforced", True, f"LIMIT {self.max_rows} appended")
        if int(match.group(1)) > self.max_rows:
            return FINAL_LIMIT.sub(f"LIMIT {self.max_rows}", query), GuardrailEvent(
                "cypher", "limit_enforced", True, f"LIMIT capped to {self.max_rows}")
        return query, GuardrailEvent("cypher", "limit_enforced", True, "LIMIT already within bounds")


# --------------------------------------------------------------------------- #
# 5. Output grounding
# --------------------------------------------------------------------------- #
def _numbers_in(rows: list[dict]) -> list[float]:
    found: list[float] = []

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            found.append(float(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(rows)
    return found


def check_grounding(answer: str, rows: list[dict]) -> tuple[GuardrailEvent, list[str]]:
    """Every airport code and number >= 10 in the answer must come from the rows.

    Numbers may be rounded by the LLM, so a 1% tolerance is accepted.
    """
    haystack = json.dumps(rows, default=str)
    numbers = _numbers_in(rows)
    ungrounded = [
        code for code in re.findall(r"\b[A-Z]{3}\b", answer)
        if code not in GROUNDING_STOPWORDS and code not in haystack
    ]
    for token in re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", answer):
        value = float(token.replace(",", ""))
        if value < 10:
            continue
        if not any(abs(value - n) <= max(1.0, 0.01 * abs(n)) for n in numbers):
            ungrounded.append(token)
    ungrounded = sorted(set(ungrounded))
    event = GuardrailEvent("output", "grounded_in_results", not ungrounded,
                           f"not found in query results: {ungrounded}" if ungrounded else "")
    return event, ungrounded
