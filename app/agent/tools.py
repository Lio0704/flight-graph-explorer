"""Vetted tools the agent can call instead of writing free-form Cypher.

Tools reuse the same services as the REST API: hand-written, parameterised,
tested queries. Their arguments are validated with Pydantic, so an LLM that
invents an argument or a bad value is rejected before anything runs.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.database import Neo4jClient
from app.models.schemas import IATA_PATTERN, CentralityMetric, Continent, normalise_code
from app.services.airports import AirportService
from app.services.network import NetworkService
from app.services.routes import RouteService


class _Strict(BaseModel):
    model_config = {"extra": "forbid"}


class ShortestRouteArgs(_Strict):
    """Arguments of shortest_route."""

    source: str = Field(..., pattern=IATA_PATTERN)
    target: str = Field(..., pattern=IATA_PATTERN)

    @field_validator("source", "target")
    @classmethod
    def _upper(cls, value: str) -> str:
        return normalise_code(value)


class ReachableArgs(_Strict):
    """Arguments of reachable."""

    iata: str = Field(..., pattern=IATA_PATTERN)
    hops: int = Field(2, ge=1, le=4)


class AirportInfoArgs(_Strict):
    """Arguments of airport_info."""

    iata: str = Field(..., pattern=IATA_PATTERN)


class CentralArgs(_Strict):
    """Arguments of central_airports."""

    metric: CentralityMetric = CentralityMetric.DEGREE
    continent: Continent | None = None
    limit: int = Field(10, ge=1, le=50)


class AirlineHubsArgs(_Strict):
    """Arguments of airline_hubs."""

    code: str = Field(..., pattern=r"^[A-Za-z0-9]{2,3}$")
    limit: int = Field(10, ge=1, le=50)


def _as_rows(result) -> list[dict]:
    if isinstance(result, list):
        return result
    return [result]


class ToolBox:
    """Registry: tool name -> (argument model, callable returning rows)."""

    def __init__(self, client: Neo4jClient) -> None:
        airports, routes, network = AirportService(client), RouteService(client), NetworkService(client)
        self._tools: dict[str, tuple[type[BaseModel], Callable[[BaseModel], object]]] = {
            "shortest_route": (ShortestRouteArgs, lambda a: routes.fewest_connections(a.source, a.target)),
            "reachable": (ReachableArgs, lambda a: airports.reachable(a.iata.upper(), a.hops)),
            "airport_info": (AirportInfoArgs, lambda a: airports.get(a.iata.upper())),
            "central_airports": (CentralArgs, lambda a: airports.central(a.metric, a.continent, a.limit)),
            "airline_hubs": (AirlineHubsArgs, lambda a: network.airline_hubs(a.code.upper(), a.limit)),
        }

    @property
    def names(self) -> list[str]:
        """Available tool names."""
        return sorted(self._tools)

    def validate(self, name: str, args: dict) -> BaseModel:
        """Return parsed arguments or raise ValueError with a readable message."""
        if name not in self._tools:
            raise ValueError(f"unknown tool '{name}', available: {self.names}")
        model, _ = self._tools[name]
        try:
            return model.model_validate(args or {})
        except ValidationError as exc:
            raise ValueError(f"invalid arguments for {name}: {exc.errors(include_url=False)}") from exc

    def run(self, name: str, args: BaseModel) -> list[dict]:
        """Execute a validated tool call and return rows."""
        _, func = self._tools[name]
        return _as_rows(func(args))
