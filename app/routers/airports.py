"""Airport endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.dependencies import get_airport_service
from app.models.schemas import (
    IATA_PATTERN,
    AirportDetail,
    AirportPage,
    AirportSearchParams,
    CentralityMetric,
    Continent,
    NearbyAirport,
    NearbyParams,
    RankedAirport,
    ReachabilityResponse,
)
from app.services.airports import AirportService

router = APIRouter(prefix="/api/airports", tags=["airports"])

IataPath = Annotated[str, Path(pattern=IATA_PATTERN, description="IATA code, e.g. CDG")]
Service = Annotated[AirportService, Depends(get_airport_service)]


@router.get("", response_model=AirportPage, summary="Search airports (paginated)")
def search_airports(params: Annotated[AirportSearchParams, Query()], service: Service) -> dict:
    """Search by name / city / IATA fragment (TEXT index) and continent, ordered by PageRank."""
    return service.search(params)


@router.get("/nearby", response_model=list[NearbyAirport], summary="Airports near a coordinate")
def nearby_airports(params: Annotated[NearbyParams, Query()], service: Service) -> list[dict]:
    """Geo search backed by the POINT index on Airport.location."""
    return service.nearby(params)


@router.get("/central", response_model=list[RankedAirport], summary="Most central airports")
def central_airports(
    service: Service,
    metric: CentralityMetric = CentralityMetric.DEGREE,
    continent: Continent | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> list[dict]:
    """Rank airports by degree (live), PageRank or betweenness (GDS, pre-computed)."""
    return service.central(metric, continent, limit)


@router.get("/{iata}", response_model=AirportDetail, summary="Airport details")
def get_airport(iata: IataPath, service: Service) -> dict:
    """Airport card with traffic counts, GDS scores and top airlines."""
    return service.get(iata.upper())


@router.get("/{iata}/reachable", response_model=ReachabilityResponse, summary="Reachability within k hops")
def reachable(iata: IataPath, service: Service, hops: Annotated[int, Query(ge=1, le=4)] = 2) -> dict:
    """Airports reachable within `hops` flights (BFS via ANY SHORTEST), grouped by level."""
    return service.reachable(iata.upper(), hops)
