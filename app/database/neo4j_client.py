"""Thin wrapper around the official Neo4j Python driver.

Every query goes through one of three entry points so the access mode is explicit:

* ``read``  -> managed READ transaction (used by the API and by the LLM agent).
  Neo4j refuses any write inside a read transaction, which is our last line of
  defence if a malicious Cypher statement ever slipped through the guardrails.
* ``write`` -> managed WRITE transaction (ingestion only).
* ``run``   -> auto-commit query (GDS procedures, ``CALL ... IN TRANSACTIONS``).
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import READ_ACCESS, WRITE_ACCESS, GraphDatabase, unit_of_work
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from app.config import Settings

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Small facade over a Neo4j driver bound to one database."""

    def __init__(self, uri: str, auth: tuple[str, str], database: str = "neo4j", **driver_options) -> None:
        self._driver = GraphDatabase.driver(uri, auth=auth, **driver_options)
        self._database = database

    @classmethod
    def from_settings(cls, settings: Settings) -> "Neo4jClient":
        """Build a client from application settings."""
        return cls(settings.neo4j_uri, (settings.neo4j_user, settings.neo4j_password), settings.neo4j_database)

    def close(self) -> None:
        """Close the underlying driver and its connection pool."""
        self._driver.close()

    def ping(self) -> bool:
        """Return True when Neo4j answers, False otherwise (never raises)."""
        try:
            self._driver.verify_connectivity()
            return True
        except (ServiceUnavailable, Neo4jError, OSError) as exc:
            logger.warning(f"Neo4j unreachable: {exc}")
            return False

    def read(self, query: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> list[dict]:
        """Run ``query`` in a READ transaction and return records as dicts."""

        def work(tx):
            return [record.data() for record in tx.run(query, params or {})]

        if timeout:
            work = unit_of_work(timeout=timeout)(work)
        with self._driver.session(database=self._database, default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)

    def write(self, query: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Run ``query`` in a WRITE transaction and return records as dicts."""

        def work(tx):
            return [record.data() for record in tx.run(query, params or {})]

        with self._driver.session(database=self._database, default_access_mode=WRITE_ACCESS) as session:
            return session.execute_write(work)

    def run(self, query: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Run an auto-commit query (needed for GDS and ``CALL {} IN TRANSACTIONS``)."""
        with self._driver.session(database=self._database) as session:
            return [record.data() for record in session.run(query, params or {})]

    def explain(self, query: str, params: dict[str, Any] | None = None) -> dict | None:
        """Compile ``query`` with EXPLAIN (nothing is executed) and return the plan.

        Raises ``Neo4jError`` if the statement is syntactically or semantically invalid.
        """
        with self._driver.session(database=self._database, default_access_mode=READ_ACCESS) as session:
            summary = session.run("EXPLAIN " + query, params or {}).consume()
            return summary.plan

    def profile(self, query: str, params: dict[str, Any] | None = None) -> tuple[int, dict | None, float]:
        """Execute ``query`` with PROFILE; return (row count, profile tree, server time in ms)."""
        with self._driver.session(database=self._database, default_access_mode=READ_ACCESS) as session:
            result = session.run("PROFILE " + query, params or {})
            rows = len(list(result))
            summary = result.consume()
            elapsed = (summary.result_available_after or 0) + (summary.result_consumed_after or 0)
            return rows, summary.profile, float(elapsed)
