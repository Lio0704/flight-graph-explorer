"""Shared fixtures: an in-memory fake Neo4j client and a FastAPI test client."""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by name

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import Neo4jError

from app.config import get_settings
from app.main import create_app

API_KEY = "test-key"


class FakeNeo4jClient:
    """Returns canned rows per query text and records every call."""

    def __init__(self) -> None:
        self.responses: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.alive = True
        self.explain_error: Exception | None = None

    def on(self, query: str, rows) -> "FakeNeo4jClient":
        """Register rows (or a callable params -> rows) for a query text."""
        self.responses[query] = rows
        return self

    def _lookup(self, query: str, params: dict | None) -> list[dict]:
        self.calls.append((query, params or {}))
        value = self.responses.get(query, [])
        if isinstance(value, Exception):
            raise value
        return value(params or {}) if callable(value) else copy.deepcopy(value)

    def read(self, query, params=None, timeout=None):
        """Fake READ transaction."""
        _ = timeout
        return self._lookup(query, params)

    def write(self, query, params=None):
        """Fake WRITE transaction."""
        return self._lookup(query, params)

    def run(self, query, params=None):
        """Fake auto-commit query."""
        return self._lookup(query, params)

    def explain(self, query, params=None):
        """Fake EXPLAIN: raises the configured error, if any."""
        self.calls.append(("EXPLAIN " + query, params or {}))
        if self.explain_error:
            raise self.explain_error

    def ping(self) -> bool:
        """Fake connectivity check."""
        return self.alive

    def close(self) -> None:
        """Nothing to close."""

    def queries(self) -> list[str]:
        """All query texts received so far."""
        return [q for q, _ in self.calls]


def neo4j_error(code: str, message: str) -> Neo4jError:
    """Build a driver exception the same way the driver does."""
    return Neo4jError._hydrate_neo4j(code=code, message=message)


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    """Deterministic settings for every test (no .env leakage)."""
    monkeypatch.setenv("API_KEY", API_KEY)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "link_predictor.json"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_db() -> FakeNeo4jClient:
    """A fresh fake database."""
    return FakeNeo4jClient()


@pytest.fixture
def client(fake_db) -> TestClient:
    """FastAPI test client wired to the fake database."""
    with TestClient(create_app(db=fake_db)) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict:
    """Headers with a valid API key."""
    return {"X-API-Key": API_KEY}


@pytest.fixture
def trained_model(tmp_path) -> str:
    """Write a tiny valid model file and return its path."""
    from app.services.link_prediction import FEATURE_NAMES  # pylint: disable=import-outside-toplevel

    payload = {
        "feature_names": FEATURE_NAMES,
        "mean": [0.0] * 6, "scale": [1.0] * 6,
        "coef": [0.5, 0.5, 0.0, 0.3, -0.5, 0.2], "intercept": -1.0,
        "metrics": {"roc_auc": 0.9}, "trained_at": "2026-10-19T00:00:00+00:00",
    }
    path = tmp_path / "link_predictor.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)
