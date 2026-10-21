"""Pathfinding and route prediction."""

from __future__ import annotations

from app.config import get_settings
from app.database import Neo4jClient
from app.exceptions import NotFoundError
from app.queries import cypher
from app.services.airports import AirportService
from app.services.gds import ensure_routes_projection, run_gds
from app.services.link_prediction import LinkPredictor, build_features


class RouteService:
    """Shortest paths (Cypher + GDS) and ML route suggestions."""

    def __init__(self, client: Neo4jClient) -> None:
        self._db = client
        self._airports = AirportService(client)

    def _response(self, source: str, target: str, algorithm: str, itineraries: list[dict]) -> dict:
        if not itineraries:
            raise NotFoundError(f"No route from {source} to {target} within the search limits")
        return {"source": source, "target": target, "algorithm": algorithm, "itineraries": itineraries}

    def fewest_connections(self, source: str, target: str, limit: int = 5) -> dict:
        """Itineraries with the minimum number of legs (BFS), shortest distance first."""
        self._airports.ensure_exists(source, target)
        rows = self._db.read(cypher.SHORTEST_BY_HOPS, {"source": source, "target": target, "limit": limit})
        return self._response(source, target, "allShortestPaths (hops)", rows)

    def shortest_distance(self, source: str, target: str) -> dict:
        """Distance-weighted shortest path with GDS Dijkstra."""
        self._airports.ensure_exists(source, target)
        graph = ensure_routes_projection(self._db)
        rows = run_gds(self._db, cypher.SHORTEST_BY_DISTANCE, {"source": source, "target": target, "graph": graph})
        return self._response(source, target, "gds.shortestPath.dijkstra (km)", rows)

    def alternatives(self, source: str, target: str, k: int = 3) -> dict:
        """k shortest itineraries by distance with GDS Yen's algorithm."""
        self._airports.ensure_exists(source, target)
        graph = ensure_routes_projection(self._db)
        rows = run_gds(
            self._db, cypher.K_SHORTEST_BY_DISTANCE, {"source": source, "target": target, "graph": graph, "k": k}
        )
        return self._response(source, target, "gds.shortestPath.yens (km)", rows)

    def suggest(self, iata: str, limit: int = 10, candidates: int = 200) -> dict:
        """Score 2-hop candidates with the link-prediction model."""
        model = LinkPredictor.load(get_settings().model_path)
        self._airports.ensure_exists(iata)
        rows = self._db.read(cypher.ROUTE_CANDIDATES, {"iata": iata, "candidates": candidates})
        scored = []
        for row in rows:
            features = build_features(
                row["common_neighbours"], row["adamic_adar"], (row["deg_a"], row["deg_b"] or 0),
                row["distance_km"], row["same_country"],
            )
            scored.append({
                "iata": row["iata"], "name": row["name"], "city": row["city"],
                "probability": round(model.predict_proba(features), 4),
                "common_neighbours": row["common_neighbours"],
                "distance_km": round(row["distance_km"], 1),
            })
        scored.sort(key=lambda item: item["probability"], reverse=True)
        return {
            "origin": iata,
            "model_version": model.version,
            "model_metrics": model.metrics,
            "suggestions": scored[:limit],
        }
