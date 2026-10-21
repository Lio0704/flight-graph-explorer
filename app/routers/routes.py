"""Pathfinding and route-prediction endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.dependencies import get_route_service
from app.models.schemas import (
    IATA_PATTERN,
    AlternativesParams,
    RoutePairParams,
    RouteResponse,
    RouteSuggestionResponse,
    ShortestParams,
)
from app.services.routes import RouteService

router = APIRouter(prefix="/api/routes", tags=["routes"])

Service = Annotated[RouteService, Depends(get_route_service)]
Pair = Annotated[RoutePairParams, Query()]


def _distinct(pair):
    if pair.source == pair.target:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "'from' and 'to' must be different airports")
    return pair


@router.get("/shortest", response_model=RouteResponse, summary="Fewest connections")
def shortest_route(pair: Annotated[ShortestParams, Query()], service: Service) -> dict:
    """All itineraries with the minimum number of legs (max 5), shortest distance first."""
    pair = _distinct(pair)
    return service.fewest_connections(pair.source, pair.target, pair.limit)


@router.get("/shortest-distance", response_model=RouteResponse, summary="Shortest distance (GDS Dijkstra)")
def shortest_distance(pair: Pair, service: Service) -> dict:
    """Distance-weighted shortest path computed on the GDS in-memory graph."""
    pair = _distinct(pair)
    return service.shortest_distance(pair.source, pair.target)


@router.get("/alternatives", response_model=RouteResponse, summary="k alternative itineraries (GDS Yen)")
def alternatives(pair: Annotated[AlternativesParams, Query()], service: Service) -> dict:
    """The k shortest loop-free itineraries by distance."""
    pair = _distinct(pair)
    return service.alternatives(pair.source, pair.target, pair.k)


@router.get("/suggest", response_model=RouteSuggestionResponse, summary="Predicted new routes (ML)")
def suggest_routes(
    service: Service,
    iata: Annotated[str, Query(pattern=IATA_PATTERN)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict:
    """Link prediction: likely future direct routes from an airport (hypotheses, not facts)."""
    return service.suggest(iata.upper(), limit)
