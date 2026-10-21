"""LangGraph workflow: question -> plan -> validate -> execute -> grounded answer.

            +-------------+
 question ->| input_guard |--blocked--------------------------------> END (rejected)
            +-------------+
                   | ok
                   v
              +---------+   refuse / LLM error
         +--->|  plan   |-------------------------------------------> END
         |    +---------+
         |         v
         |    +----------+  invalid & attempts left
         +----| validate |-----------+
   (feedback) +----------+           | invalid & no attempts left -> END (rejected)
         ^         | valid
         |         v
         |    +---------+  DB error & attempts left
         +----| execute |
              +---------+
                   v
              +---------+
              | answer  |  LLM summary + grounding check + confidence
              +---------+
                   v
                  END

Every step appends to `trace` and `guardrails`, both returned to the client so
the reasoning path is fully visible.
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from neo4j.exceptions import Neo4jError

from app.agent import prompts
from app.agent.guardrails import CypherGuard, GuardrailEvent, GuardrailViolation, check_grounding, check_question
from app.agent.llm import LLMClient, LLMError, parse_json_object
from app.agent.tools import ToolBox
from app.config import Settings
from app.database import Neo4jClient
from app.exceptions import NotFoundError


class AgentState(TypedDict, total=False):
    """State flowing through the graph. Lists annotated with operator.add are appended to."""

    question: str
    attempts: int
    plan: dict
    errors: list[str]
    cypher: str | None
    tool: str | None
    tool_args: Any
    rows: list[dict]
    answer: str
    status: str
    grounded: bool
    confidence: float
    guardrails: Annotated[list[dict], operator.add]
    trace: Annotated[list[str], operator.add]


def _events(events: list[GuardrailEvent]) -> list[dict]:
    return [e.as_dict() for e in events]


class GraphAgent:
    """NL -> (tool | Cypher) agent over the flight graph."""

    def __init__(self, client: Neo4jClient, llm: LLMClient, settings: Settings) -> None:
        self._db = client
        self._llm = llm
        self._settings = settings
        self._guard = CypherGuard(max_rows=settings.agent_max_rows)
        self._tools = ToolBox(client)
        self._graph = self._build()

    # ------------------------------------------------------------------ nodes
    def input_guard(self, state: AgentState) -> dict:
        """Reject empty, oversized or prompt-injection questions before calling the LLM."""
        events = check_question(state["question"])
        blocked = not all(e.passed for e in events)
        return {
            "guardrails": _events(events),
            "status": "rejected" if blocked else "running",
            "answer": "Question rejected by input guardrails." if blocked else "",
            "attempts": 0,
            "errors": [],
            "trace": ["input_guard: " + ("blocked" if blocked else "ok")],
        }

    def plan(self, state: AgentState) -> dict:
        """Ask the LLM for a JSON plan (tool call, Cypher, or refusal)."""
        attempts = state.get("attempts", 0) + 1
        user = f"Question: {state['question']}"
        if state.get("errors"):
            user = prompts.RETRY_TEMPLATE.format(
                previous=json.dumps(state.get("plan", {})), errors="; ".join(state["errors"])
            ) + "\n\n" + user
        try:
            plan = parse_json_object(self._llm.complete(prompts.PLANNER_SYSTEM, user, json_mode=True))
        except LLMError as exc:
            return {"attempts": attempts, "status": "failed", "answer": str(exc), "trace": [f"plan#{attempts}: {exc}"]}
        action = plan.get("action")
        update: dict = {"attempts": attempts, "plan": plan, "trace": [f"plan#{attempts}: {json.dumps(plan)[:300]}"]}
        if action == "refuse":
            update.update(status="refused", answer=f"I can't answer this from the graph: {plan.get('reasoning', '')}")
        return update

    def validate(self, state: AgentState) -> dict:
        """Validate the plan: Pydantic for tools, static guard + EXPLAIN for Cypher."""
        plan = state["plan"]
        action = plan.get("action")
        if action == "tool":
            try:
                args = self._tools.validate(plan.get("tool", ""), plan.get("args", {}))
            except ValueError as exc:
                event = GuardrailEvent("tool", "valid_tool_call", False, str(exc))
                return self._invalid([event], [str(exc)])
            event = GuardrailEvent("tool", "valid_tool_call", True, plan["tool"])
            return {"tool": plan["tool"], "tool_args": args, "cypher": None, "errors": [],
                    "guardrails": _events([event]), "trace": [f"validate: tool {plan['tool']} ok"]}
        if action != "cypher" or not isinstance(plan.get("cypher"), str):
            event = GuardrailEvent("plan", "known_action", False, f"unexpected plan: {action!r}")
            return self._invalid([event], [event.detail])
        try:
            query, events = self._guard.check(plan["cypher"])
        except GuardrailViolation as violation:
            return self._invalid(violation.events, [str(violation)])
        try:
            self._db.explain(query)
            events.append(GuardrailEvent("cypher", "explain_compiles", True, "EXPLAIN ok"))
        except Neo4jError as exc:
            events.append(GuardrailEvent("cypher", "explain_compiles", False, exc.message or str(exc)))
            return self._invalid(events, [f"Neo4j rejected the query: {exc.message}"])
        return {"cypher": query, "tool": None, "errors": [], "guardrails": _events(events),
                "trace": ["validate: cypher ok"]}

    def _invalid(self, events: list[GuardrailEvent], errors: list[str]) -> dict:
        failed = [e for e in events if not e.passed] or events
        return {"errors": errors, "guardrails": _events(failed), "trace": [f"validate: rejected ({errors[0][:200]})"]}

    def execute(self, state: AgentState) -> dict:
        """Run the vetted tool, or the Cypher in a READ transaction with timeout."""
        try:
            if state.get("tool"):
                rows = self._tools.run(state["tool"], state["tool_args"])
            else:
                rows = self._db.read(state["cypher"], timeout=self._settings.agent_query_timeout_s)
        except NotFoundError as exc:
            rows = []
            return {"rows": rows, "errors": [], "trace": [f"execute: not found ({exc})"]}
        except Neo4jError as exc:
            event = GuardrailEvent("execution", "read_only_transaction", False, exc.message or str(exc))
            return {"rows": [], "errors": [f"execution failed: {exc.message}"], "guardrails": _events([event]),
                    "trace": ["execute: failed"]}
        rows = rows[: self._settings.agent_max_rows]
        event = GuardrailEvent("execution", "read_only_transaction", True, f"{len(rows)} rows")
        return {"rows": rows, "errors": [], "guardrails": _events([event]), "trace": [f"execute: {len(rows)} rows"]}

    def answer(self, state: AgentState) -> dict:
        """Summarise the rows with the LLM, then check grounding and score confidence."""
        rows = state.get("rows", [])
        if not rows:
            answer = "No matching data was found in the graph for this question."
            grounded, events = True, []
        else:
            shown = rows[:30]
            user = prompts.ANSWER_TEMPLATE.format(
                question=state["question"],
                source=f"tool {state['tool']}" if state.get("tool") else "Cypher query",
                count=len(rows),
                truncated=", first 30 shown" if len(rows) > 30 else "",
                rows=json.dumps(shown, default=str),
            )
            try:
                answer = self._llm.complete(prompts.ANSWER_SYSTEM, user).strip()
            except LLMError as exc:
                answer = f"(LLM summary unavailable: {exc}) See verified rows."
            event, _ = check_grounding(answer, rows)
            grounded, events = event.passed, [event]
        confidence = 0.9 if state.get("tool") else 0.75
        confidence -= 0.15 * (state.get("attempts", 1) - 1)
        confidence -= 0.0 if rows else 0.3
        confidence -= 0.0 if grounded else 0.35
        return {
            "answer": answer, "grounded": grounded, "status": "answered" if rows else "no_results",
            "confidence": round(max(0.05, min(confidence, 1.0)), 2),
            "guardrails": _events(events), "trace": [f"answer: grounded={grounded}"],
        }

    # ---------------------------------------------------------------- routing
    def _after_input(self, state: AgentState) -> str:
        return "plan" if state["status"] == "running" else END

    def _after_plan(self, state: AgentState) -> str:
        return "validate" if state.get("status") == "running" else END

    def _after_validate(self, state: AgentState) -> str:
        if not state.get("errors"):
            return "execute"
        return "plan" if state["attempts"] < self._settings.agent_max_attempts else "give_up"

    def _after_execute(self, state: AgentState) -> str:
        if state.get("errors"):
            return "plan" if state["attempts"] < self._settings.agent_max_attempts else "give_up"
        return "answer"

    @staticmethod
    def give_up(state: AgentState) -> dict:
        """Stop after too many invalid plans."""
        return {
            "status": "rejected",
            "answer": "The generated query was rejected by the guardrails: " + "; ".join(state.get("errors", [])),
            "rows": [], "confidence": 0.0, "trace": ["give_up"],
        }

    def _build(self):
        builder = StateGraph(AgentState)
        builder.add_node("input_guard", self.input_guard)
        builder.add_node("plan", self.plan)
        builder.add_node("validate", self.validate)
        builder.add_node("execute", self.execute)
        builder.add_node("answer", self.answer)
        builder.add_node("give_up", self.give_up)
        builder.add_edge(START, "input_guard")
        builder.add_conditional_edges("input_guard", self._after_input, ["plan", END])
        builder.add_conditional_edges("plan", self._after_plan, ["validate", END])
        builder.add_conditional_edges("validate", self._after_validate, ["execute", "plan", "give_up"])
        builder.add_conditional_edges("execute", self._after_execute, ["answer", "plan", "give_up"])
        builder.add_edge("answer", END)
        builder.add_edge("give_up", END)
        return builder.compile()

    # -------------------------------------------------------------- interface
    def ask(self, question: str) -> dict:
        """Run the workflow and shape the public response."""
        state = self._graph.invoke({"question": question, "guardrails": [], "trace": []})
        return {
            "question": question,
            "status": state.get("status", "failed"),
            "answer": state.get("answer", ""),
            "answer_is_ai_generated": True,
            "grounded": state.get("grounded", False),
            "confidence": state.get("confidence", 0.0),
            "verified_data": {
                "source": "neo4j",
                "tool": state.get("tool"),
                "cypher": state.get("cypher"),
                "rows": state.get("rows", []),
            },
            "attempts": state.get("attempts", 0),
            "llm": self._llm.name,
            "guardrails": state.get("guardrails", []),
            "trace": state.get("trace", []),
        }

    def validate_cypher(self, query: str) -> dict:
        """Run only the Cypher guardrails (used by the demo endpoint)."""
        try:
            safe, events = self._guard.check(query)
        except GuardrailViolation as violation:
            return {"valid": False, "query": None, "checks": _events(violation.events)}
        try:
            self._db.explain(safe)
            events.append(GuardrailEvent("cypher", "explain_compiles", True, "EXPLAIN ok"))
        except Neo4jError as exc:
            events.append(GuardrailEvent("cypher", "explain_compiles", False, exc.message or str(exc)))
            return {"valid": False, "query": safe, "checks": _events(events)}
        return {"valid": True, "query": safe, "checks": _events(events)}
