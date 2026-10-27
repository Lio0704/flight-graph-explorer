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
* Reachability within k hops. Enumerating **paths** with `[:ROUTE*1..k]` is O(d^k): average degree
  d ≈ 6 but hubs have > 200 neighbours. **Our benchmark disproved our first assumption** about the
  fix, though: when the query only asks for `count(DISTINCT dst)`, Neo4j's planner already rewrites
  the variable-length expand into a *pruning* breadth-first expand (`VarLengthExpand(Pruning)`),
  which visits each airport once. That version was faster (≈35k db hits, 6 ms) than our
  `ANY SHORTEST` query (≈47k db hits, 237 ms), which finds one shortest path *per destination*.
  We keep `ANY SHORTEST` in the endpoint because it also returns the exact hop level of each
  airport (the pruning form only returns the set of reachable airports), and 237 ms is acceptable
  for k ≤ 4. Lesson: measure with PROFILE instead of assuming. The path explosion is real only when
  paths are returned or aggregated without `DISTINCT`.
* `allShortestPaths` is bounded to 5 hops and limited to `$limit` results.
* All variable-length patterns (API and LLM) are bounded; the agent guardrail rejects unbounded ones.

### 8. Why a graph and not a relational database?

The reference question for this subject: *"Which airports are reachable from CDG within four connections?"*

**Relational version** (tables `airports(iata PK)` and `routes(src, dst)` with an index on `routes(src)`):
```sql
WITH RECURSIVE reach(iata, hops) AS (
    SELECT dst, 1 FROM routes WHERE src = 'CDG'
  UNION
    SELECT r.dst, reach.hops + 1
    FROM reach JOIN routes r ON r.src = reach.iata
    WHERE reach.hops < 4
)
SELECT iata, MIN(hops) AS hops FROM reach WHERE iata <> 'CDG' GROUP BY iata;
```

**Graph version** (what `/api/airports/{iata}/reachable` runs):
```cypher
MATCH (src:Airport {iata: 'CDG'})
MATCH p = ANY SHORTEST (src)-[:ROUTE]->{1,4}(dst:Airport)
RETURN dst.iata, length(p) AS hops
```

| | Relational (recursive CTE) | Neo4j |
|---|---|---|
| One hop | a **join**: B-tree lookup in the `routes` index, O(log E) per airport | follow the airport's own relationship pointers ("index-free adjacency"), O(1) per route |
| k hops | k self-joins; the work depends on the size of the `routes` table at each level | the work depends only on the part of the graph actually explored, not on the total size |
| Duplicates | `UNION` only removes identical `(iata, hops)` rows, so an airport reached at hop 2 is expanded again at hops 3 and 4; avoiding that needs extra bookkeeping | `ANY SHORTEST` (or the planner's pruning expand) visits each airport once |
| Paths | the route itself is lost unless you also accumulate an array of visited codes, and cycles must be blocked by hand | the path `p` is a first-class value; `shortestPath`, Dijkstra and Yen are built in |
| Expressiveness | about 10 lines, the recursion is easy to get wrong | 2 lines that read like the question |

**Honest caveat:** with 6k airports and 37k routes, PostgreSQL would also answer this in milliseconds.
The graph's advantage grows with **depth**, **graph size** and **how much the question is about
paths** (shortest routes, centrality, communities). That is exactly what this project does, which
is why we chose Neo4j and not a SQL table of routes.

---

## Appendix: anti-genericity comparison

**Method.** On 22 Oct 2026 we gave a fresh AI assistant (new conversation, no project context) this prompt:

> Design a Neo4j graph schema for a "Global Flight Network Explorer" project using the OpenFlights
> dataset (airports.dat, routes.dat). Entities: Airports, Airlines, Routes. Relationships: ROUTE,
> OPERATED_BY. Properties: distance, country, continent, airport codes.

**AI-generated baseline (summary of its answer):**
```
(:Airport {airport_id (unique), iata (nullable, indexed), icao, name, city, country, continent,
           location, altitude_ft, timezone, utc_offset, dst, type})
(:Airline {airline_id (unique), name, iata, icao, callsign, country, active})
(:Route   {route_key (unique) = airline-src-dst, airline_code, codeshare, stops, equipment, distance_km})

(:Route)-[:OPERATED_BY]->(:Airline)
(:Route)-[:FROM]->(:Airport)     (:Route)-[:TO]->(:Airport)
(:Airport)-[:ROUTE {distance_km, airlines, airline_count, domestic, intercontinental}]->(:Airport)
```
Loaded with `LOAD CSV` row by row; continent from the timezone prefix, `'Other'` otherwise.

The baseline is reasonable: it already notices that a relationship cannot point to another
relationship, so it keeps **two layers** (one `:Route` node per airline route + an aggregated
`ROUTE` relationship). Our decisions differ on the points below.

| Aspect | AI baseline | Our model | Why we differ |
|---|---|---|---|
| Airline ↔ route | 67k `:Route` nodes + `FROM`/`TO`/`OPERATED_BY` (≈200k extra relationships) **and** the aggregated `ROUTE` | aggregated `ROUTE` only, airline codes in `r.airlines`, plus derived `(:Airline)-[:SERVES {route_count}]->(:Airport)` (~19k) | Two layers store every route twice and must be kept consistent on every reload. No endpoint needs per-airline equipment or codeshare detail, and airline questions ("hubs of AF") are answered by `SERVES` with an index seek (see `make benchmark`). **Trade-off accepted:** we lose equipment per airline (we keep the union per pair). |
| Airport key | `airport_id` unique, `iata` nullable with a plain index | `iata` **unique constraint**, 1,626 airports without IATA dropped | Every API call and the LLM use IATA codes. A plain index does not prevent duplicates, and OpenFlights has duplicate IATA codes, so `{iata: 'XXX'}` could return two airports. Cost measured, not guessed: only 729 of 67,663 route rows (1.1%) are dropped because an endpoint is unknown (cleaning stats logged by the seeder). |
| Country | string property on Airport and Airline; `:Country` only as an "optional extension" | `:Country` node shared by airports (`IN_COUNTRY`) and airlines (`REGISTERED_IN`), no string copy | One source of truth for spelling; domestic vs international becomes a pattern (`/api/countries/{c}/connectivity`) instead of string comparisons. |
| Continent | timezone prefix, `Atlantic/*` and `Indian/*` fall into `'Other'` | explicit rules for ocean timezones (`Atlantic/Reykjavik` → Europe, `Atlantic/Bermuda` → Americas, `Pacific/Honolulu` → Americas), then majority continent of the same country | `'Other'` would break the `/central?continent=` and per-continent component endpoints for Iceland, the Azores, Canaries, Maldives, Mauritius... |
| Derived booleans | `domestic`, `intercontinental` on `ROUTE` | not stored | Considered and rejected: they duplicate information already in the graph (`IN_COUNTRY`, `continent`) and go stale if a country mapping is corrected. Computed in the query when needed. |
| Ingestion | `LOAD CSV` + `MERGE` per row in Cypher | Python ETL (tested functions) + `UNWIND` batches of 2,000 | Cleaning rules (null markers, duplicate codes, self-loops, unknown airports) are unit-tested and reported; batches are one transaction per 2,000 rows. |
| Indexes | constraints on ids, plain indexes on iata / country / continent, POINT index | 3 constraints + 8 indexes incl. **TEXT** (search) and indexes on GDS outputs | Each index maps to an endpoint (table in §6). |
| Analytics | mentions GDS "for distance-weighted paths or centrality" | PageRank, betweenness, Louvain written back; Dijkstra / Yen / WCC on projections; link prediction | Pre-computed scores let the API rank airports with an index instead of recomputing per request. |

**What we kept from the baseline:** the directed `ROUTE` between airports as the traversal layer,
`location` as a point, continent derived from the timezone, IATA/ICAO on airlines.
**What we changed and why:** single source of truth (no duplicated route layer, Country as a node),
a key that matches how the data is queried, and cleaning rules that do not silently produce `'Other'`.
