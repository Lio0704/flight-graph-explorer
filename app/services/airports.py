"""Airport-centric graph queries."""

from __future__ import annotations

from app.database import Neo4jClient
from app.exceptions import NotFoundError
from app.models.schemas import AirportSearchParams, CentralityMetric, Continent, NearbyParams
from app.queries import cypher


class AirportService:
    """Read-only queries about airports."""

    def __init__(self, client: Neo4jClient) -> None:
        self._db = client

    def get(self, iata: str) -> dict:
        """Return the airport card or raise NotFoundError."""
        rows = self._db.read(cypher.AIRPORT_DETAIL, {"iata": iata})
        if not rows:
            raise NotFoundError(f"Airport '{iata}' not found")
        return rows[0]

    def ensure_exists(self, *codes: str) -> None:
        """Raise NotFoundError for the first unknown IATA code."""
        found = {r["iata"] for r in self._db.read(cypher.AIRPORTS_EXIST, {"codes": list(codes)})}
        for code in codes:
            if code not in found:
                raise NotFoundError(f"Airport '{code}' not found")

    def search(self, params: AirportSearchParams) -> dict:
        """Paginated search by text fragment and/or continent."""
        page_query, count_query = cypher.airport_search_query(bool(params.search), params.continent is not None)
        query_params = {
            "q": (params.search or "").lower(),
            "continent": params.continent.value if params.continent else None,
            "skip": params.skip,
            "limit": params.limit,
        }
        total = self._db.read(count_query, query_params)[0]["total"]
        items = self._db.read(page_query, query_params)
        return {"total": total, "skip": params.skip, "limit": params.limit, "items": items}

    def nearby(self, params: NearbyParams) -> list[dict]:
        """Airports within a radius of a coordinate (POINT index)."""
        return self._db.read(cypher.AIRPORTS_NEARBY, {
            "lat": params.lat, "lon": params.lon, "radius_m": params.radius_km * 1000, "limit": params.limit,
        })

    def reachable(self, iata: str, hops: int, sample: int = 10) -> dict:
        """Airports reachable within ``hops`` connections, grouped by first-reach level."""
        self.ensure_exists(iata)
        levels = self._db.read(cypher.REACHABLE_BY_HOPS[hops], {"iata": iata, "sample": sample})
        return {
            "origin": iata,
            "max_hops": hops,
            "total_reachable": sum(level["airports"] for level in levels),
            "levels": levels,
        }

    def central(self, metric: CentralityMetric, continent: Continent | None, limit: int) -> list[dict]:
        """Rank airports by a centrality metric, optionally within one continent."""
        query = cypher.central_airports_query(metric.value, continent is not None)
        return self._db.read(query, {"continent": continent.value if continent else None, "limit": limit})
