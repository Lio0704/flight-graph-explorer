"""FastAPI dependencies: database client, services, API-key authentication."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import get_settings
from app.database import Neo4jClient
from app.services.airports import AirportService
from app.services.network import NetworkService
from app.services.routes import RouteService

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_db(request: Request) -> Neo4jClient:
    """The Neo4j client created at startup (overridden in tests)."""
    return request.app.state.db


def get_airport_service(db: Neo4jClient = Depends(get_db)) -> AirportService:
    """Airport service bound to the request's client."""
    return AirportService(db)


def get_route_service(db: Neo4jClient = Depends(get_db)) -> RouteService:
    """Route service bound to the request's client."""
    return RouteService(db)


def get_network_service(db: Neo4jClient = Depends(get_db)) -> NetworkService:
    """Network service bound to the request's client."""
    return NetworkService(db)


def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    """Protect sensitive endpoints (LLM calls cost money) with a shared API key.

    `secrets.compare_digest` gives a constant-time comparison (no timing attack).
    """
    expected = get_settings().api_key
    if not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid X-API-Key header")
