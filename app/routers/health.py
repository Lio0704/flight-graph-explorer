"""Health and global statistics."""

from fastapi import APIRouter, Depends, Response, status

from app import __version__
from app.database import Neo4jClient
from app.dependencies import get_db, get_network_service
from app.models.schemas import HealthResponse, NetworkStats
from app.services.network import NetworkService

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness and Neo4j connectivity")
def health(response: Response, db: Neo4jClient = Depends(get_db)) -> dict:
    """Return 200 when Neo4j answers, 503 otherwise (used by Docker healthchecks)."""
    neo4j_ok = db.ping()
    if not neo4j_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if neo4j_ok else "degraded", "neo4j": neo4j_ok, "version": __version__}


@router.get("/api/stats", response_model=NetworkStats, summary="Node and relationship counts")
def stats(service: NetworkService = Depends(get_network_service)) -> dict:
    """Global counts of airports, routes, airlines and countries."""
    return service.stats()
