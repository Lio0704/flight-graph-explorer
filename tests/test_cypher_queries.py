"""Static Cypher tests: every query of the API must respect the schema and our conventions.

These run without a database. `test_integration.py` runs the same queries for real.
"""

import re

import pytest

from app.agent.guardrails import CypherGuard, GuardrailViolation, strip_literals
from app.queries import cypher
from app.queries.schema import CONSTRAINTS, INDEXES, NODE_LABELS, RELATIONSHIP_TYPES
from scripts import seed_data

READ_QUERIES = {
    name: value for name, value in vars(cypher).items()
    if name.isupper() and not name.startswith("_") and isinstance(value, str) and "gds." not in value
}
READ_QUERIES.update({f"REACHABLE_{k}": q for k, q in cypher.REACHABLE_BY_HOPS.items()})
for _metric in cypher.CENTRALITY_EXPRESSIONS:
    READ_QUERIES[f"CENTRAL_{_metric}"] = cypher.central_airports_query(_metric, True)
for _text in (True, False):
    for _cont in (True, False):
        READ_QUERIES[f"SEARCH_{_text}_{_cont}"] = cypher.airport_search_query(_text, _cont)[0]


@pytest.mark.parametrize("name", sorted(READ_QUERIES))
def test_api_queries_pass_the_agent_guardrails(name):
    """Our own read queries must satisfy the rules we impose on the LLM."""
    try:
        CypherGuard(max_rows=1000).check(READ_QUERIES[name])
    except GuardrailViolation as violation:
        pytest.fail(f"{name}: {violation}")


@pytest.mark.parametrize("name", sorted(READ_QUERIES))
def test_queries_are_parameterised(name):
    """No f-string leftovers or quoted user values: every input is a $parameter."""
    code = strip_literals(READ_QUERIES[name])
    assert "{}" not in code
    assert "''" not in code or name in {"AIRLINE_OVERLAP"}


def test_list_queries_are_limited():
    for name in ["AIRPORTS_NEARBY", "SHORTEST_BY_HOPS", "COMMUNITIES", "AIRLINE_HUBS", "ROUTE_CANDIDATES"]:
        assert re.search(r"\bLIMIT \$\w+", getattr(cypher, name)), name


def test_reachable_hops_are_bounded():
    assert sorted(cypher.REACHABLE_BY_HOPS) == [1, 2, 3, 4]
    assert "{1,4}" in cypher.REACHABLE_BY_HOPS[4]


def test_search_query_builder():
    page, count = cypher.airport_search_query(True, False)
    assert "a.search_text CONTAINS $q" in page and "$continent" not in page
    assert count.strip().startswith("MATCH (a:Airport) WHERE")
    page, _ = cypher.airport_search_query(False, False)
    assert "WHERE" not in page.split("WITH", maxsplit=1)[0]


def test_central_query_rejects_unknown_metric():
    with pytest.raises(KeyError):
        cypher.central_airports_query("DROP", False)


def test_schema_statements_are_idempotent():
    for statement in CONSTRAINTS + INDEXES:
        assert "IF NOT EXISTS" in statement


def test_constraints_cover_merge_keys():
    """Every MERGE in the seed script must hit a uniqueness constraint (otherwise it is a full scan)."""
    merges = re.findall(r"MERGE \((\w+):(\w+) \{(\w+):", " ".join([
        seed_data.UPSERT_COUNTRIES, seed_data.UPSERT_AIRPORTS, seed_data.UPSERT_AIRLINES]))
    constrained = {(m[0], m[1]) for m in re.findall(r"FOR \(\w+:(\w+)\) REQUIRE \w+\.(\w+) IS UNIQUE",
                                                    " ".join(CONSTRAINTS))}
    for _, label, key in merges:
        assert (label, key) in constrained


def test_seed_queries_use_schema():
    text = " ".join([seed_data.UPSERT_AIRPORTS, seed_data.UPSERT_ROUTES, seed_data.UPSERT_SERVES,
                     seed_data.UPSERT_AIRLINES])
    labels = set(re.findall(r"\(\w*:(\w+)", text))
    rels = set(re.findall(r"\[\w*:(\w+)\]", text)) | set(re.findall(r"-\[:(\w+)\]->", text))
    assert labels <= set(NODE_LABELS)
    assert rels <= set(RELATIONSHIP_TYPES)


def test_batches():
    assert [len(b) for b in seed_data.batches(list(range(5)), 2)] == [2, 2, 1]
