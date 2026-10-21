"""Run Graph Data Science algorithms and write results back as node properties.

Projections (in-memory, built from the stored graph):
  routes             directed, weighted by distance_km   -> PageRank, betweenness, Dijkstra/Yen in the API
  routes_undirected  undirected                          -> Louvain communities, WCC stats

Why these algorithms:
  * PageRank: an airport is important if important airports fly to it
    (better than raw degree, which over-rates regional feeder airports).
  * Betweenness: airports that sit on many shortest paths = transfer chokepoints.
  * Louvain: groups of airports more connected to each other than to the rest,
    i.e. airline/regional networks; compared with continents in the notebook.
  * WCC: how many disconnected islands the network has.
"""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.database import Neo4jClient
from app.queries.cypher import GDS_DROP, GDS_PROJECT_ROUTES

logger = logging.getLogger("gds")

ROUTES_GRAPH = "routes"
UNDIRECTED_GRAPH = "routes_undirected"

PROJECT_UNDIRECTED = """
CALL gds.graph.project($name, 'Airport', {ROUTE: {orientation: 'UNDIRECTED'}})
YIELD graphName, nodeCount, relationshipCount
RETURN graphName, nodeCount, relationshipCount
"""

PAGERANK = """
CALL gds.pageRank.write($name, {writeProperty: 'pagerank', maxIterations: 20, dampingFactor: 0.85})
YIELD nodePropertiesWritten, ranIterations, didConverge
RETURN nodePropertiesWritten, ranIterations, didConverge
"""

BETWEENNESS = """
CALL gds.betweenness.write($name, {writeProperty: 'betweenness', samplingSize: 2000, samplingSeed: 42})
YIELD nodePropertiesWritten, centralityDistribution
RETURN nodePropertiesWritten, centralityDistribution.max AS max_score
"""

LOUVAIN = """
CALL gds.louvain.write($name, {writeProperty: 'community'})
YIELD communityCount, modularity
RETURN communityCount, modularity
"""

WCC_STATS = """
CALL gds.wcc.stats($name)
YIELD componentCount, componentDistribution
RETURN componentCount, componentDistribution.max AS largest_component
"""


def run_analytics(client: Neo4jClient) -> dict:
    """Project the graph, run the algorithms and return a summary."""
    for name in (ROUTES_GRAPH, UNDIRECTED_GRAPH):
        client.run(GDS_DROP, {"name": name})
    summary = {
        "projection": client.run(GDS_PROJECT_ROUTES, {"name": ROUTES_GRAPH})[0],
        "projection_undirected": client.run(PROJECT_UNDIRECTED, {"name": UNDIRECTED_GRAPH})[0],
    }
    summary["pagerank"] = client.run(PAGERANK, {"name": ROUTES_GRAPH})[0]
    summary["betweenness"] = client.run(BETWEENNESS, {"name": ROUTES_GRAPH})[0]
    summary["louvain"] = client.run(LOUVAIN, {"name": UNDIRECTED_GRAPH})[0]
    summary["wcc"] = client.run(WCC_STATS, {"name": UNDIRECTED_GRAPH})[0]
    # Keep `routes` in memory for the API's Dijkstra / Yen endpoints; free the other one.
    client.run(GDS_DROP, {"name": UNDIRECTED_GRAPH})
    return summary


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    client = Neo4jClient.from_settings(get_settings())
    try:
        summary = run_analytics(client)
        logger.info(json.dumps(summary, indent=2, default=str))
    finally:
        client.close()


if __name__ == "__main__":
    main()
