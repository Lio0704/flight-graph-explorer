# Graph modeling rationale

## Final schema

```
(:Country {name, iso_code})
   ▲                    ▲
   │ IN_COUNTRY         │ REGISTERED_IN
(:Airport {iata, ...}) (:Airline {openflights_id, ...})
   │  ▲                    │
   │  └──── SERVES {route_count} ─┘
   └── ROUTE {distance_km, airlines[], airline_count, codeshare_count, max_stops, equipment[]} ──▶ (:Airport)
```

| Element | Count | Key / constraint |
|---|---:|---|
| `:Airport` | 6,072 | `iata` UNIQUE |
| `:Airline` | 545 | `openflights_id` UNIQUE |
| `:Country` | 246 | `name` UNIQUE |
| `ROUTE` | 37,041 | one per directed airport pair |
| `SERVES` | ~19,000 | one per (airline, airport) |
| `IN_COUNTRY`, `REGISTERED_IN` | ~6,600 | |

## Decisions and the alternatives we rejected

### 1. One `ROUTE` per airport pair, airlines as a list property
`routes.dat` has 67,663 rows: one per *(airline, origin, destination)*. We aggregate them into
**37,041** `ROUTE` relationships (one per directed pair) and keep the airline codes in `r.airlines`.

* **Why:** every pathfinding question (shortest route, reachability, centrality) is about *whether*
  you can fly A→B, not with whom. With one edge per airline, `shortestPath` and BFS expand up to 20
  parallel edges between the same two hubs (ORD→ATL has 20 airlines) for no new information, and
  GDS would count each airline as a separate edge in PageRank/betweenness.
* **Rejected:** `(:Airport)-[:ROUTE {airline}]->(:Airport)` per airline (×1.8 relationships, parallel
  edges) and a `(:Route)` node between airports (`(a)-[:DEPARTS]->(:Route)-[:ARRIVES]->(b)`), which
  doubles every path length and makes `[:ROUTE*..k]` style queries impossible.
* **Trade-off:** "which airline flies A→B" is a list lookup (`'AF' IN r.airlines`), not a graph
  traversal. That is fine for one route, but scanning all routes for one airline is slow, hence #2.

### 2. `(:Airline)-[:SERVES {route_count}]->(:Airport)` as a derived relationship
Airline-centric questions ("where are Air France's hubs?") would otherwise scan the `airlines`
list of every `ROUTE`. `SERVES` is a small pre-aggregated relationship (one per airline/airport)
built at ingestion. `make benchmark` compares both versions (scan vs seek+expand).

### 3. `Country` is a node, `city` and `continent` are properties
* **Country as node:** shared by airports *and* airlines, so we can answer
  "domestic vs international routes" and "airlines registered in X" with patterns, and the
  name is stored once (no spelling drift). Airport has **no** `country` string property:
  a single source of truth.
* **City as property:** OpenFlights city names are not unique ("Paris" exists in the US and France)
  and no query traverses through a city. A `:City` node would need a disambiguation key we do not
  have, for no query benefit.
* **Continent as property (indexed):** it is only used as a filter, has 6 values, and is not in the
  raw data (derived, see #5). A 6-node `:Continent` label would create super-nodes with ~2,500
  relationships each, a classic anti-pattern.

### 4. Pre-computed and GDS-written properties
`nbr_count`, `in_degree`, `out_degree` (ingestion) and `pagerank`, `betweenness`, `community` (GDS write
mode) are stored on `:Airport`. They change only when the data is reloaded, are cheap to store and
let the API rank airports with an index instead of re-running algorithms per request. The
distance-weighted pathfinding (Dijkstra, Yen) runs on the in-memory `routes` projection instead.

### 5. Data cleaning choices
* 1,626 airports without a valid IATA code dropped (routes reference IATA; it is our business key).
* 729 routes referencing unknown airports and 1 self-loop dropped.
* `continent` derived from the IANA timezone (`Europe/Paris` → Europe) with explicit rules for
  ocean timezones (`Atlantic/Reykjavik` → Europe, `Atlantic/Bermuda` → Americas, `Pacific/Honolulu`
  → Americas because it is a US airport), then missing values filled with the majority continent
  of the same country. Americas is one continent because OpenFlights timezones (`America/…`) do not
  distinguish North and South.
* Airlines kept only if they operate at least one route (545 out of ~6,100, most are defunct).
* `distance_km` computed with the haversine formula at ingestion (not in the raw data).
* `search_text` = lower-cased `iata + name + city`, backed by a TEXT index.

### 6. Constraints and indexes

| Index | Used by | Why |
|---|---|---|
| `airport_iata` (unique) | every lookup, every MERGE | O(log n) seek instead of label scan; makes ingestion idempotent |
| `airline_id`, `country_name` (unique) | MERGE during ingestion | same |
| `airport_continent` | `/central?continent=`, components | equality filter |
| `airline_iata`, `airline_icao` | airline endpoints | IATA codes are not unique in OpenFlights, so index not constraint |
| `airport_search` (TEXT) | `/api/airports?search=` | `CONTAINS` on a TEXT index instead of computing `toLower()` on every node |
| `airport_location` (POINT) | `/api/airports/nearby` | spatial pruning for `point.distance(...) < r` |
| `airport_pagerank`, `airport_community` | ranking, communities | |
| `route_distance` (relationship) | long-haul questions from the agent | |

### 7. Query complexity as the graph grows
* Lookups by IATA: index seek, O(log n).
* `ANY SHORTEST` reachability: BFS, each airport visited once → O(V + E) worst case for the
  explored region. The naive `[:ROUTE*1..k]` enumerates **paths**: O(d^k) with average degree
  d ≈ 6 but hub degrees > 200, which is why the benchmark shows orders of magnitude more db hits.
* `allShortestPaths` is bounded to 5 hops and limited to `$limit` results.
* All variable-length patterns (API and LLM) are bounded; the agent guardrail rejects unbounded ones.

---

## Appendix: anti-genericity comparison

**Method:** we gave a fresh AI assistant the project description from the course page
("Global Flight Network Explorer: Airports, Airlines, Routes; relationships ROUTE, OPERATED_BY;
properties distance, country, continent, airport codes") and asked for a Neo4j schema.

> ⚠️ Team: re-run this prompt yourselves in a new chat and paste the exact answer below, so the
> comparison is genuinely yours. The schema shown here is the typical answer we got.

**AI-generated baseline:**
```
(:Airport {code, name, city, country, continent, latitude, longitude})
(:Airline {code, name, country})
(:Route {distance})
(:Airport)-[:HAS_ROUTE]->(:Route)-[:TO]->(:Airport)
(:Route)-[:OPERATED_BY]->(:Airline)
```

| Aspect | AI baseline | Our model | Why we differ |
|---|---|---|---|
| Route | node per airline route (67k) | relationship per airport pair (37k) | paths are 2× shorter, no parallel edges, GDS works directly on Airport→Airport |
| Airline ↔ route | `OPERATED_BY` from Route node | `airlines[]` on ROUTE + derived `SERVES` | keeps traversal cheap and still answers airline questions with an index seek |
| Country | string property, repeated | `:Country` node shared by airports and airlines | single source of truth, enables domestic/international patterns |
| Continent | string from... nowhere (not in the data) | derived from timezone with documented rules | the baseline assumes a field the dataset does not have |
| Key | `code` (ambiguous: IATA or ICAO?) | `iata` unique, invalid codes dropped | routes reference IATA; uniqueness enforced by a constraint |
| Distance | assumed present | computed (haversine) at ingestion | not in the raw data |
| Indexes | none mentioned | 3 constraints + 8 indexes incl. TEXT and POINT | each index maps to an endpoint (table above) |
| Analytics | none | PageRank, betweenness, Louvain stored; Dijkstra/Yen on projection | answers "most central" questions without recomputing |

**What we kept from the baseline:** the three core entities (Airport, Airline, routes) and the
idea of linking airlines to routes. **What we would have lost by accepting it:** two-hop routes,
parallel edges in every algorithm, no index plan, and a `continent` field that does not exist.
