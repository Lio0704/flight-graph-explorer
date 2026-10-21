"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from app import __version__
from app.agent.llm import LLMError
from app.config import get_settings
from app.database import Neo4jClient
from app.exceptions import GdsUnavailableError, ModelNotTrainedError, NotFoundError
from app.routers import agent, airports, health, network, routes

logger = logging.getLogger("app")

DESCRIPTION = """
Graph-backed exploration of the world air-route network (OpenFlights, ~6k airports, ~37k routes).

* **airports**: search, geo search, centrality, k-hop reachability
* **routes**: fewest connections (Cypher), shortest distance and alternatives (GDS), ML route prediction
* **network**: connected components, Louvain communities, airline and country analytics
* **agent**: natural language -> guarded Cypher / tool calls (requires `X-API-Key`)
"""


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Open the Neo4j driver at startup and close it at shutdown."""
    if not hasattr(application.state, "db"):
        application.state.db = Neo4jClient.from_settings(get_settings())
    yield
    application.state.db.close()


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


def create_app(db: Neo4jClient | None = None) -> FastAPI:
    """Build the application. Tests inject a fake `db`."""
    application = FastAPI(
        title="Flight Graph Explorer", version=__version__, description=DESCRIPTION, lifespan=lifespan
    )
    if db is not None:
        application.state.db = db

    for router in (health.router, airports.router, routes.router, network.router, agent.router):
        application.include_router(router)

    @application.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, str(exc))

    @application.exception_handler(ModelNotTrainedError)
    async def model_missing(_: Request, exc: ModelNotTrainedError) -> JSONResponse:
        return _error(503, str(exc))

    @application.exception_handler(GdsUnavailableError)
    async def gds_missing(_: Request, exc: GdsUnavailableError) -> JSONResponse:
        return _error(503, str(exc))

    @application.exception_handler(ServiceUnavailable)
    async def db_down(_: Request, exc: ServiceUnavailable) -> JSONResponse:
        logger.error(f"Neo4j unavailable: {exc}")
        return _error(503, "Graph database unavailable, try again later")

    @application.exception_handler(Neo4jError)
    async def db_error(_: Request, exc: Neo4jError) -> JSONResponse:
        logger.error(f"Neo4j error {exc.code}: {exc.message}")
        return _error(500, "Graph query failed")

    @application.exception_handler(LLMError)
    async def llm_error(_: Request, exc: LLMError) -> JSONResponse:
        return _error(502, str(exc))

    return application


app = create_app()
