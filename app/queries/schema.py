"""Graph schema: constraints, indexes, and the description given to the LLM.

Keeping the schema in one module means the ingestion script, the API, the
guardrails (allow-lists) and the LLM prompt can never drift apart.
"""

CONSTRAINTS = [
    # Business keys. A uniqueness constraint also creates a backing index, so
    # every `MATCH (a:Airport {iata: $x})` is an index seek and MERGE is safe.
    "CREATE CONSTRAINT airport_iata IF NOT EXISTS FOR (a:Airport) REQUIRE a.iata IS UNIQUE",
    "CREATE CONSTRAINT airline_id IF NOT EXISTS FOR (l:Airline) REQUIRE l.openflights_id IS UNIQUE",
    "CREATE CONSTRAINT country_name IF NOT EXISTS FOR (c:Country) REQUIRE c.name IS UNIQUE",
]

INDEXES = [
    # Filter used by /central, /components and the communities endpoint.
    "CREATE INDEX airport_continent IF NOT EXISTS FOR (a:Airport) ON (a.continent)",
    # Airline lookup by code (IATA is not unique in OpenFlights, so no constraint).
    "CREATE INDEX airline_iata IF NOT EXISTS FOR (l:Airline) ON (l.iata)",
    "CREATE INDEX airline_icao IF NOT EXISTS FOR (l:Airline) ON (l.icao)",
    # Full-text-like search: TEXT index makes `CONTAINS` an index scan.
    "CREATE TEXT INDEX airport_search IF NOT EXISTS FOR (a:Airport) ON (a.search_text)",
    # Geo queries: POINT index serves `point.distance(a.location, $p) < r`.
    "CREATE POINT INDEX airport_location IF NOT EXISTS FOR (a:Airport) ON (a.location)",
    # Properties written by GDS, used for ranking and community lookups.
    "CREATE INDEX airport_pagerank IF NOT EXISTS FOR (a:Airport) ON (a.pagerank)",
    "CREATE INDEX airport_community IF NOT EXISTS FOR (a:Airport) ON (a.community)",
    # Relationship property index (long-haul queries).
    "CREATE INDEX route_distance IF NOT EXISTS FOR ()-[r:ROUTE]-() ON (r.distance_km)",
]

# --- Allow-lists used by the Cypher guardrail -------------------------------
NODE_LABELS = {
    "Airport": [
        "iata", "icao", "openflights_id", "name", "city", "latitude", "longitude", "altitude_ft",
        "timezone", "continent", "type", "location", "search_text", "out_degree", "in_degree",
        "nbr_count", "pagerank", "betweenness", "community",
    ],
    "Airline": ["openflights_id", "name", "iata", "icao", "callsign", "active"],
    "Country": ["name", "iso_code"],
}

RELATIONSHIP_TYPES = {
    "ROUTE": ["distance_km", "airlines", "airline_count", "codeshare_count", "max_stops", "equipment"],
    "SERVES": ["route_count"],
    "IN_COUNTRY": [],
    "REGISTERED_IN": [],
}

ALLOWED_PROPERTIES = sorted({p for props in [*NODE_LABELS.values(), *RELATIONSHIP_TYPES.values()] for p in props})

CONTINENTS = ["Africa", "Americas", "Antarctica", "Asia", "Europe", "Oceania"]

SCHEMA_FOR_LLM = """
Node labels and properties:
  (:Airport {iata: STRING (unique, e.g. 'CDG'), icao, name, city, latitude, longitude,
             altitude_ft, timezone, continent: one of Africa|Americas|Antarctica|Asia|Europe|Oceania,
             type, out_degree: INT, in_degree: INT, nbr_count: INT (distinct connected airports),
             pagerank: FLOAT, betweenness: FLOAT, community: INT (Louvain cluster id)})
  (:Airline {openflights_id: INT (unique), name, iata, icao, callsign, active: BOOLEAN})
  (:Country {name: STRING (unique, English name e.g. 'France', 'United States'), iso_code})

Relationships:
  (:Airport)-[:ROUTE {distance_km: FLOAT, airlines: LIST<STRING> of airline codes,
                      airline_count: INT, codeshare_count: INT, max_stops: INT,
                      equipment: LIST<STRING>}]->(:Airport)
      One ROUTE per directed airport pair, aggregated over all airlines flying it.
  (:Airline)-[:SERVES {route_count: INT}]->(:Airport)
  (:Airport)-[:IN_COUNTRY]->(:Country)
  (:Airline)-[:REGISTERED_IN]->(:Country)

Notes:
  - There is NO Flight, Route, City or Continent node: city and continent are Airport properties.
  - Airport has no `country` property: use the IN_COUNTRY relationship.
""".strip()
