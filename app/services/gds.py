"""Helpers around Graph Data Science projections."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from neo4j.exceptions import ClientError

from app.database import Neo4jClient
from app.exceptions import GdsUnavailableError
from app.queries import cypher

ROUTES_GRAPH = "routes"


def _translate(exc: ClientError) -> GdsUnavailableError:
    message = str(exc)
    if "ProcedureNotFound" in (exc.code or "") or "Unknown function 'gds" in message:
        return GdsUnavailableError("Graph Data Science plugin is not installed in Neo4j.")
    return GdsUnavailableError(f"GDS error: {exc.message or message}")


def ensure_routes_projection(client: Neo4jClient) -> str:
    """Create the shared `routes` in-memory graph if it does not exist yet."""
    try:
        exists = client.run(cypher.GDS_GRAPH_EXISTS, {"name": ROUTES_GRAPH})[0]["exists"]
        if not exists:
            client.run(cypher.GDS_PROJECT_ROUTES, {"name": ROUTES_GRAPH})
    except ClientError as exc:
        raise _translate(exc) from exc
    return ROUTES_GRAPH


def run_gds(client: Neo4jClient, query: str, params: dict) -> list[dict]:
    """Run a GDS query, translating plugin errors."""
    try:
        return client.run(query, params)
    except ClientError as exc:
        raise _translate(exc) from exc


@contextmanager
def temporary_projection(client: Neo4jClient, query: str, params: dict):
    """Create a uniquely named projection and always drop it afterwards."""
    name = f"tmp_{uuid.uuid4().hex[:12]}"
    run_gds(client, query, {**params, "name": name})
    try:
        yield name
    finally:
        client.run(cypher.GDS_DROP, {"name": name})
