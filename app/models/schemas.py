"""Pydantic models for request validation and response serialisation."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator

IATA_PATTERN = r"^[A-Za-z0-9]{3}$"


def normalise_code(value: str) -> str:
    """Upper-case and strip an airport / airline code."""
    return value.strip().upper()


class Continent(str, Enum):
    """Continents as derived during ingestion."""

    AFRICA = "Africa"
    AMERICAS = "Americas"
    ANTARCTICA = "Antarctica"
    ASIA = "Asia"
    EUROPE = "Europe"
    OCEANIA = "Oceania"


class CentralityMetric(str, Enum):
    """Whitelisted centrality metrics (maps to Cypher expressions)."""

    DEGREE = "degree"
    PAGERANK = "pagerank"
    BETWEENNESS = "betweenness"


# --------------------------------------------------------------------------- #
# Query-parameter models (keep endpoint signatures small and validated)
# --------------------------------------------------------------------------- #
class Pagination(BaseModel):
    """Common pagination parameters."""

    skip: int = Field(0, ge=0, le=10_000)
    limit: int = Field(20, ge=1, le=100)


class AirportSearchParams(Pagination):
    """Filters for GET /api/airports."""

    search: str | None = Field(None, min_length=2, max_length=60, description="name, city or IATA fragment")
    continent: Continent | None = None


class NearbyParams(BaseModel):
    """Filters for GET /api/airports/nearby."""

    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    radius_km: float = Field(100, gt=0, le=1000)
    limit: int = Field(10, ge=1, le=50)


class RoutePairParams(BaseModel):
    """Origin / destination pair."""

    source: str = Field(..., pattern=IATA_PATTERN, alias="from", description="origin IATA, e.g. CDG")
    target: str = Field(..., pattern=IATA_PATTERN, alias="to", description="destination IATA, e.g. SYD")

    @field_validator("source", "target")
    @classmethod
    def _upper(cls, value: str) -> str:
        return normalise_code(value)


class ShortestParams(RoutePairParams):
    """Origin / destination + number of itineraries."""

    limit: int = Field(5, ge=1, le=10)


class AlternativesParams(RoutePairParams):
    """Origin / destination + k for Yen's algorithm."""

    k: int = Field(3, ge=1, le=10)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    """Liveness + Neo4j connectivity."""

    status: str
    neo4j: bool
    version: str


class AirportSummary(BaseModel):
    """Compact airport representation used in lists."""

    iata: str
    name: str | None = None
    city: str | None = None
    country: str | None = None
    continent: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class AirlineRef(BaseModel):
    """Airline serving an airport."""

    name: str | None = None
    code: str | None = None
    routes: int | None = None


class AirportDetail(AirportSummary):
    """Full airport card."""

    icao: str | None = None
    altitude_ft: int | None = None
    timezone: str | None = None
    pagerank: float | None = None
    betweenness: float | None = None
    community: int | None = None
    departures: int
    arrivals: int
    top_airlines: list[AirlineRef] = []


class AirportPage(BaseModel):
    """Paginated airport list."""

    total: int
    skip: int
    limit: int
    items: list[AirportSummary]


class NearbyAirport(AirportSummary):
    """Airport with its distance to the query point."""

    distance_km: float


class RankedAirport(AirportSummary):
    """Airport with a centrality score."""

    score: float


class HopLevel(BaseModel):
    """Airports first reachable at exactly `hops` connections."""

    hops: int
    airports: int
    sample: list[str]


class ReachabilityResponse(BaseModel):
    """Result of a k-hop reachability query."""

    origin: str
    max_hops: int
    total_reachable: int
    levels: list[HopLevel]


class Itinerary(BaseModel):
    """One path through the network."""

    path: list[str]
    hops: int
    total_km: float
    airlines_per_leg: list[list[str]] | None = None


class RouteResponse(BaseModel):
    """One or more itineraries between two airports."""

    source: str
    target: str
    algorithm: str
    itineraries: list[Itinerary]


class RouteSuggestion(BaseModel):
    """ML-predicted route (a hypothesis, not an existing flight)."""

    iata: str
    name: str | None = None
    city: str | None = None
    probability: float
    common_neighbours: int
    distance_km: float
    status: str = "prediction"


class RouteSuggestionResponse(BaseModel):
    """Ranked suggestions + model metadata."""

    origin: str
    model_version: str
    model_metrics: dict
    disclaimer: str = "Predicted by a statistical model from network structure; these routes do NOT exist today."
    suggestions: list[RouteSuggestion]


class Component(BaseModel):
    """A weakly connected component."""

    component_id: int
    size: int
    sample: list[str]


class ComponentsResponse(BaseModel):
    """Connected components of (part of) the network."""

    scope: str
    component_count: int
    largest_component_size: int
    isolated_airports: int
    components: list[Component]


class Community(BaseModel):
    """Louvain community summary."""

    community: int
    size: int
    top_hubs: list[str]
    by_continent: list[dict]


class AirlineHub(BaseModel):
    """Airport ranked by number of routes an airline operates there."""

    airline: str | None = None
    iata: str
    name: str | None = None
    city: str | None = None
    routes: int


class AirlineOverlap(BaseModel):
    """Shared airports and competing routes between two airlines."""

    first_airline: str | None = None
    second_airline: str | None = None
    shared_airport_count: int
    shared_airports: list[str]
    competing_route_count: int
    competing_routes: list[str]


class CountryConnectivity(BaseModel):
    """Outbound connectivity of a country."""

    country: str
    airport_count: int
    total_routes: int
    domestic_routes: int
    domestic_share: float
    top_destinations: list[dict]


class NetworkStats(BaseModel):
    """Global counts."""

    airports: int
    routes: int
    airlines: int
    countries: int
