"""All Cypher used by the API, in one place.

Conventions
-----------
* Every user value is passed as a parameter ($name), never concatenated.
  This prevents Cypher injection and lets Neo4j cache the query plan.
* The only string formatting below uses values from fixed Python whitelists
  (hop count 1..4, metric names), never raw user input.
* Every query that can return many rows ends with LIMIT (and SKIP for pagination).
"""

# --------------------------------------------------------------------------- #
# Health / stats
# --------------------------------------------------------------------------- #
NETWORK_STATS = """
RETURN COUNT { (:Airport) }          AS airports,
       COUNT { ()-[:ROUTE]->() }     AS routes,
       COUNT { (:Airline) }          AS airlines,
       COUNT { (:Country) }          AS countries
"""

# --------------------------------------------------------------------------- #
# Airports
# --------------------------------------------------------------------------- #
_AIRPORT_PROJECTION = """
       a.iata AS iata, a.name AS name, a.city AS city, a.continent AS continent,
       [(a)-[:IN_COUNTRY]->(c:Country) | c.name][0] AS country,
       a.latitude AS latitude, a.longitude AS longitude
"""

AIRPORTS_EXIST = "MATCH (a:Airport) WHERE a.iata IN $codes RETURN a.iata AS iata"

AIRPORT_DETAIL = """
MATCH (a:Airport {iata: $iata})
RETURN a.iata AS iata, a.icao AS icao, a.name AS name, a.city AS city,
       [(a)-[:IN_COUNTRY]->(c:Country) | c.name][0] AS country,
       a.continent AS continent, a.latitude AS latitude, a.longitude AS longitude,
       a.altitude_ft AS altitude_ft, a.timezone AS timezone,
       a.pagerank AS pagerank, a.betweenness AS betweenness, a.community AS community,
       COUNT { (a)-[:ROUTE]->() } AS departures,
       COUNT { (a)<-[:ROUTE]-() } AS arrivals,
       COLLECT {
           MATCH (a)<-[s:SERVES]-(l:Airline)
           WITH l, s ORDER BY s.route_count DESC LIMIT 10
           RETURN {name: l.name, code: coalesce(l.iata, l.icao), routes: s.route_count} AS airline
       } AS top_airlines
"""


def airport_search_query(with_text: bool, with_continent: bool) -> tuple[str, str]:
    """Build the (page query, count query) pair for airport search.

    Only fixed WHERE fragments are combined, so the query text stays injection-free
    and Neo4j can still use the TEXT index (search_text) and RANGE index (continent).
    """
    conditions = []
    if with_text:
        conditions.append("a.search_text CONTAINS $q")
    if with_continent:
        conditions.append("a.continent = $continent")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    page = f"""
MATCH (a:Airport) {where}
WITH a ORDER BY coalesce(a.pagerank, 0) DESC, a.iata
SKIP $skip LIMIT $limit
RETURN {_AIRPORT_PROJECTION}
"""
    count = f"MATCH (a:Airport) {where} RETURN count(a) AS total"
    return page, count


AIRPORTS_NEARBY = f"""
WITH point({{latitude: $lat, longitude: $lon}}) AS here
MATCH (a:Airport)
WHERE point.distance(a.location, here) < $radius_m
WITH a, round(point.distance(a.location, here) / 1000.0, 1) AS distance_km
ORDER BY distance_km
LIMIT $limit
RETURN {_AIRPORT_PROJECTION}, distance_km
"""

# Reachability: GQL quantified path + ANY SHORTEST, one shortest path per destination,
# so we get each airport's exact hop level. See docs/benchmarks.md: for a plain count, the
# planner's pruning VarLengthExpand on `[:ROUTE*1..k]` + DISTINCT is faster.
_REACHABLE_TEMPLATE = """
MATCH (src:Airport {{iata: $iata}})
MATCH p = ANY SHORTEST (src)-[:ROUTE]->{{1,{hops}}}(dst:Airport)
WITH src, dst, length(p) AS hops
WHERE dst <> src
WITH hops, dst ORDER BY hops, coalesce(dst.pagerank, 0) DESC
RETURN hops, count(dst) AS airports, collect(dst.iata)[0..$sample] AS sample
ORDER BY hops
"""
REACHABLE_BY_HOPS = {k: _REACHABLE_TEMPLATE.format(hops=k) for k in range(1, 5)}

# Naive version, kept for the benchmark only (do not use in endpoints).
REACHABLE_NAIVE_3 = """
MATCH (src:Airport {iata: $iata})-[:ROUTE*1..3]->(dst:Airport)
WHERE dst <> src
RETURN count(DISTINCT dst) AS airports
"""

# --------------------------------------------------------------------------- #
# Centrality
# --------------------------------------------------------------------------- #
# metric name -> Cypher expression. Whitelist: the API validates the metric with an Enum.
CENTRALITY_EXPRESSIONS = {
    "degree": "COUNT { (a)-[:ROUTE]-() }",
    "pagerank": "a.pagerank",
    "betweenness": "a.betweenness",
}


def central_airports_query(metric: str, with_continent: bool) -> str:
    """Return the ranking query for a whitelisted centrality metric."""
    expression = CENTRALITY_EXPRESSIONS[metric]
    where = "WHERE a.continent = $continent" if with_continent else ""
    return f"""
MATCH (a:Airport) {where}
WITH a, {expression} AS score
WHERE score IS NOT NULL
ORDER BY score DESC
LIMIT $limit
RETURN {_AIRPORT_PROJECTION}, score
"""


# --------------------------------------------------------------------------- #
# Routes / pathfinding
# --------------------------------------------------------------------------- #
# Fewest connections first (BFS), then shortest distance among those.
SHORTEST_BY_HOPS = """
MATCH (a:Airport {iata: $source})
MATCH (b:Airport {iata: $target})
MATCH p = allShortestPaths((a)-[:ROUTE*..5]->(b))
WITH p, reduce(d = 0.0, r IN relationships(p) | d + r.distance_km) AS total_km
ORDER BY total_km
LIMIT $limit
RETURN [n IN nodes(p) | n.iata] AS path,
       length(p) AS hops,
       round(total_km, 1) AS total_km,
       [r IN relationships(p) | r.airlines[0..5]] AS airlines_per_leg
"""

GDS_GRAPH_EXISTS = "CALL gds.graph.exists($name) YIELD exists RETURN exists"

GDS_PROJECT_ROUTES = """
CALL gds.graph.project($name, 'Airport', {ROUTE: {properties: 'distance_km'}})
YIELD graphName, nodeCount, relationshipCount
RETURN graphName, nodeCount, relationshipCount
"""

GDS_DROP = "CALL gds.graph.drop($name, false) YIELD graphName RETURN graphName"

# Weighted shortest path (Dijkstra) on the in-memory projection.
SHORTEST_BY_DISTANCE = """
MATCH (s:Airport {iata: $source})
MATCH (t:Airport {iata: $target})
CALL gds.shortestPath.dijkstra.stream($graph, {
    sourceNode: s, targetNode: t, relationshipWeightProperty: 'distance_km'
})
YIELD totalCost, nodeIds, costs
RETURN [nid IN nodeIds | gds.util.asNode(nid).iata] AS path,
       size(nodeIds) - 1 AS hops,
       round(totalCost, 1) AS total_km
"""

# k alternative itineraries (Yen's algorithm).
K_SHORTEST_BY_DISTANCE = """
MATCH (s:Airport {iata: $source})
MATCH (t:Airport {iata: $target})
CALL gds.shortestPath.yens.stream($graph, {
    sourceNode: s, targetNode: t, k: $k, relationshipWeightProperty: 'distance_km'
})
YIELD index, totalCost, nodeIds
RETURN [nid IN nodeIds | gds.util.asNode(nid).iata] AS path,
       size(nodeIds) - 1 AS hops,
       round(totalCost, 1) AS total_km
ORDER BY index
"""

# --------------------------------------------------------------------------- #
# Network structure (GDS)
# --------------------------------------------------------------------------- #
# Cypher projection restricted to one continent (intra-continent routes only).
# OPTIONAL MATCH keeps airports that have no intra-continent route as isolated nodes.
GDS_PROJECT_CONTINENT = """
MATCH (s:Airport) WHERE s.continent = $continent
OPTIONAL MATCH (s)-[:ROUTE]->(t:Airport) WHERE t.continent = $continent
WITH gds.graph.project($name, s, t) AS g
RETURN g.nodeCount AS nodes, g.relationshipCount AS relationships
"""

GDS_WCC_STREAM = """
CALL gds.wcc.stream($name) YIELD nodeId, componentId
WITH componentId, collect(gds.util.asNode(nodeId).iata) AS members
RETURN componentId AS component_id, size(members) AS size, members[0..$sample] AS sample
ORDER BY size DESC
"""

COMMUNITIES = """
MATCH (a:Airport) WHERE a.community IS NOT NULL
WITH a.community AS community, a.continent AS continent, count(*) AS n
WITH community, sum(n) AS size, collect({continent: continent, airports: n}) AS by_continent
ORDER BY size DESC
LIMIT $limit
CALL (community) {
    MATCH (h:Airport) WHERE h.community = community
    WITH h ORDER BY h.pagerank DESC LIMIT 5
    RETURN collect(h.iata) AS top_hubs
}
RETURN community, size, top_hubs, by_continent
"""

# --------------------------------------------------------------------------- #
# Airlines
# --------------------------------------------------------------------------- #
AIRLINE_HUBS = """
MATCH (l:Airline)
WHERE l.iata = $code OR l.icao = $code
MATCH (l)-[s:SERVES]->(a:Airport)
WITH l, a, s ORDER BY s.route_count DESC
LIMIT $limit
RETURN l.name AS airline, a.iata AS iata, a.name AS name, a.city AS city, s.route_count AS routes
"""

# Naive equivalent (benchmark only): scans every ROUTE and its airline list.
AIRLINE_HUBS_NAIVE = """
MATCH (a:Airport)-[r:ROUTE]-()
WHERE $code IN r.airlines
RETURN a.iata AS iata, count(r) AS routes
ORDER BY routes DESC
LIMIT $limit
"""

# Complex pattern: two airlines meeting at shared airports, then the routes both fly.
AIRLINE_OVERLAP = """
MATCH (l1:Airline)-[:SERVES]->(ap:Airport)<-[:SERVES]-(l2:Airline)
WHERE (l1.iata = $first OR l1.icao = $first) AND (l2.iata = $second OR l2.icao = $second)
WITH l1, l2, collect(DISTINCT ap) AS shared
CALL (shared) {
    UNWIND shared AS x
    MATCH (x)-[r:ROUTE]->(y:Airport)
    WHERE $first IN r.airlines AND $second IN r.airlines
    RETURN collect(x.iata + '-' + y.iata) AS competing_routes
}
RETURN l1.name AS first_airline, l2.name AS second_airline,
       size(shared) AS shared_airport_count,
       [a IN shared | a.iata][0..$sample] AS shared_airports,
       size(competing_routes) AS competing_route_count,
       competing_routes[0..$sample] AS competing_routes
"""

# --------------------------------------------------------------------------- #
# Countries
# --------------------------------------------------------------------------- #
COUNTRY_EXISTS = "MATCH (c:Country {name: $country}) RETURN c.name AS name"

# WITH chaining + subquery + aggregation + UNWIND + list comprehension.
COUNTRY_CONNECTIVITY = """
MATCH (c:Country {name: $country})
WITH c, COUNT { (c)<-[:IN_COUNTRY]-(:Airport) } AS airport_count
MATCH (c)<-[:IN_COUNTRY]-(a:Airport)
CALL (a) {
    MATCH (a)-[:ROUTE]->(:Airport)-[:IN_COUNTRY]->(dc:Country)
    RETURN dc.name AS dest_country, count(*) AS routes
}
WITH airport_count, dest_country, sum(routes) AS routes
WITH airport_count, collect({country: dest_country, routes: routes}) AS dests, sum(routes) AS total
WITH airport_count, dests, total,
     coalesce(head([x IN dests WHERE x.country = $country | x.routes]), 0) AS domestic
UNWIND dests AS d
WITH airport_count, total, domestic, d ORDER BY d.routes DESC
RETURN airport_count, total AS total_routes, domestic AS domestic_routes,
       collect(d)[0..$limit] AS top_destinations
"""

# --------------------------------------------------------------------------- #
# Link prediction candidates (features for the ML model)
# --------------------------------------------------------------------------- #
# Candidates = airports 2 hops away (undirected) with no direct route.
# Features match app.services.link_prediction.build_features exactly.
ROUTE_CANDIDATES = """
MATCH (a:Airport {iata: $iata})-[:ROUTE]-(m:Airport)
WITH a, collect(DISTINCT m) AS neighbours
UNWIND neighbours AS m
MATCH (m)-[:ROUTE]-(c:Airport)
WHERE c <> a AND NOT c IN neighbours
WITH a, size(neighbours) AS deg_a, c, collect(DISTINCT m) AS commons
WITH a, deg_a, c, size(commons) AS common_neighbours,
     reduce(s = 0.0, x IN commons | s + 1.0 / log(x.nbr_count)) AS adamic_adar
ORDER BY common_neighbours DESC
LIMIT $candidates
RETURN c.iata AS iata, c.name AS name, c.city AS city,
       deg_a, c.nbr_count AS deg_b, common_neighbours, adamic_adar,
       point.distance(a.location, c.location) / 1000.0 AS distance_km,
       EXISTS { (a)-[:IN_COUNTRY]->(:Country)<-[:IN_COUNTRY]-(c) } AS same_country
"""
