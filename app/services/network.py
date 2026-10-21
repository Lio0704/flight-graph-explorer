"""Network-level analytics: components, communities, airlines, countries."""

from __future__ import annotations

from app.database import Neo4jClient
from app.exceptions import NotFoundError
from app.models.schemas import Continent
from app.queries import cypher
from app.services.gds import ensure_routes_projection, run_gds, temporary_projection


class NetworkService:
    """Queries over the whole graph or large parts of it."""

    def __init__(self, client: Neo4jClient) -> None:
        self._db = client

    def stats(self) -> dict:
        """Global node / relationship counts (served from the count store)."""
        return self._db.read(cypher.NETWORK_STATS)[0]

    def components(self, continent: Continent | None, limit: int = 10, sample: int = 8) -> dict:
        """Weakly connected components, globally or for intra-continent routes only."""
        if continent is None:
            graph = ensure_routes_projection(self._db)
            rows = run_gds(self._db, cypher.GDS_WCC_STREAM, {"name": graph, "sample": sample})
            scope = "world"
        else:
            with temporary_projection(self._db, cypher.GDS_PROJECT_CONTINENT, {"continent": continent.value}) as name:
                rows = run_gds(self._db, cypher.GDS_WCC_STREAM, {"name": name, "sample": sample})
            scope = continent.value
        return {
            "scope": scope,
            "component_count": len(rows),
            "largest_component_size": rows[0]["size"] if rows else 0,
            "isolated_airports": sum(1 for r in rows if r["size"] == 1),
            "components": rows[:limit],
        }

    def communities(self, limit: int = 10) -> list[dict]:
        """Largest Louvain communities (written by `make analytics`)."""
        return self._db.read(cypher.COMMUNITIES, {"limit": limit})

    def airline_hubs(self, code: str, limit: int = 10) -> list[dict]:
        """Airports where an airline operates the most routes."""
        rows = self._db.read(cypher.AIRLINE_HUBS, {"code": code, "limit": limit})
        if not rows:
            raise NotFoundError(f"Airline '{code}' not found or has no routes")
        return rows

    def airline_overlap(self, first: str, second: str, sample: int = 20) -> dict:
        """Shared airports and competing routes of two airlines."""
        rows = self._db.read(cypher.AIRLINE_OVERLAP, {"first": first, "second": second, "sample": sample})
        if not rows:
            raise NotFoundError(f"No shared airport between '{first}' and '{second}' (or unknown airline)")
        return rows[0]

    def country_connectivity(self, country: str, limit: int = 10) -> dict:
        """Domestic vs international routes and top destination countries."""
        if not self._db.read(cypher.COUNTRY_EXISTS, {"country": country}):
            raise NotFoundError(f"Country '{country}' not found")
        rows = self._db.read(cypher.COUNTRY_CONNECTIVITY, {"country": country, "limit": limit})
        if not rows:
            return {"country": country, "airport_count": 0, "total_routes": 0, "domestic_routes": 0,
                    "domestic_share": 0.0, "top_destinations": []}
        row = rows[0]
        share = row["domestic_routes"] / row["total_routes"] if row["total_routes"] else 0.0
        return {**row, "country": country, "domestic_share": round(share, 4)}
