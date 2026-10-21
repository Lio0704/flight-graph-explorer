"""Network analytics, airline and country endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.dependencies import get_network_service
from app.models.schemas import (
    AirlineHub,
    AirlineOverlap,
    Community,
    ComponentsResponse,
    Continent,
    CountryConnectivity,
    normalise_code,
)
from app.services.network import NetworkService

router = APIRouter(tags=["network"])

Service = Annotated[NetworkService, Depends(get_network_service)]
AirlineCode = Annotated[str, Path(pattern=r"^[A-Za-z0-9]{2,3}$", description="IATA (2) or ICAO (3) code")]


@router.get("/api/network/components", response_model=ComponentsResponse, summary="Connected components (GDS WCC)")
def components(
    service: Service,
    continent: Continent | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict:
    """Islands of the network. With `continent`, only intra-continent routes are considered."""
    return service.components(continent, limit)


@router.get("/api/network/communities", response_model=list[Community], summary="Louvain communities")
def communities(service: Service, limit: Annotated[int, Query(ge=1, le=50)] = 10) -> list[dict]:
    """Largest communities with their hubs and continent mix."""
    return service.communities(limit)


@router.get("/api/airlines/{code}/hubs", response_model=list[AirlineHub], summary="Airline hubs")
def airline_hubs(code: AirlineCode, service: Service, limit: Annotated[int, Query(ge=1, le=50)] = 10) -> list[dict]:
    """Airports where the airline operates the most routes (SERVES relationship)."""
    return service.airline_hubs(normalise_code(code), limit)


@router.get("/api/airlines/{code}/overlap/{other}", response_model=AirlineOverlap, summary="Airline competition")
def airline_overlap(code: AirlineCode, other: AirlineCode, service: Service) -> dict:
    """Shared airports and routes flown by both airlines."""
    return service.airline_overlap(normalise_code(code), normalise_code(other))


@router.get("/api/countries/{country}/connectivity", response_model=CountryConnectivity, summary="Country links")
def country_connectivity(
    country: Annotated[str, Path(min_length=2, max_length=60)],
    service: Service,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict:
    """Domestic share and top destination countries for a country's airports."""
    return service.country_connectivity(country, limit)
