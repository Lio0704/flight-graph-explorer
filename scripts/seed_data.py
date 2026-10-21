"""Load the cleaned OpenFlights dataset into Neo4j.

Usage:
    python -m scripts.seed_data              # create schema + upsert everything
    python -m scripts.seed_data --if-empty   # skip if airports already exist (docker seeder)
    python -m scripts.seed_data --reset      # wipe the graph first

Bulk import strategy: rows are sent in batches with `UNWIND $rows AS row`, i.e.
one round-trip and one transaction per 2,000 rows instead of one per row. MERGE
on constrained keys makes the script idempotent (safe to re-run).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from app.config import get_settings
from app.database import Neo4jClient
from app.queries.schema import CONSTRAINTS, INDEXES
from scripts.etl import load_dataset

logger = logging.getLogger("seed")

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
BATCH_SIZE = 2000

UPSERT_COUNTRIES = """
UNWIND $rows AS row
MERGE (c:Country {name: row.name})
SET c.iso_code = row.iso_code
"""

UPSERT_AIRPORTS = """
UNWIND $rows AS row
MERGE (a:Airport {iata: row.iata})
SET a.icao = row.icao, a.openflights_id = row.openflights_id, a.name = row.name,
    a.city = row.city, a.latitude = row.latitude, a.longitude = row.longitude,
    a.altitude_ft = row.altitude_ft, a.timezone = row.timezone, a.continent = row.continent,
    a.type = row.type, a.search_text = row.search_text,
    a.location = point({latitude: row.latitude, longitude: row.longitude})
WITH a, row
MATCH (c:Country {name: row.country})
MERGE (a)-[:IN_COUNTRY]->(c)
"""

UPSERT_AIRLINES = """
UNWIND $rows AS row
MERGE (l:Airline {openflights_id: row.openflights_id})
SET l.name = row.name, l.iata = row.iata, l.icao = row.icao,
    l.callsign = row.callsign, l.active = row.active
WITH l, row
MATCH (c:Country {name: row.country})
MERGE (l)-[:REGISTERED_IN]->(c)
"""

UPSERT_ROUTES = """
UNWIND $rows AS row
MATCH (s:Airport {iata: row.src})
MATCH (d:Airport {iata: row.dst})
MERGE (s)-[r:ROUTE]->(d)
SET r.distance_km = row.distance_km, r.airlines = row.airlines, r.airline_count = row.airline_count,
    r.codeshare_count = row.codeshare_count, r.max_stops = row.max_stops, r.equipment = row.equipment
"""

UPSERT_SERVES = """
UNWIND $rows AS row
MATCH (l:Airline {openflights_id: row.airline_id})
MATCH (a:Airport {iata: row.iata})
MERGE (l)-[s:SERVES]->(a)
SET s.route_count = row.route_count
"""

# Pre-computed degrees: cheap to store, used for ranking and as ML features.
COMPUTE_DEGREES = """
MATCH (a:Airport)
OPTIONAL MATCH (a)-[:ROUTE]-(b:Airport)
WITH a, count(DISTINCT b) AS neighbours
SET a.nbr_count = neighbours,
    a.out_degree = COUNT { (a)-[:ROUTE]->() },
    a.in_degree = COUNT { (a)<-[:ROUTE]-() }
"""

COUNT_AIRPORTS = "MATCH (a:Airport) RETURN count(a) AS n"
WIPE = "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS"


def batches(rows: list, size: int = BATCH_SIZE):
    """Yield successive chunks of ``rows``."""
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def create_schema(client: Neo4jClient) -> None:
    """Create constraints first (they back MERGE), then secondary indexes."""
    for statement in CONSTRAINTS + INDEXES:
        client.run(statement)
    client.run("CALL db.awaitIndexes(300)")


def load_rows(client: Neo4jClient, label: str, query: str, rows: list) -> None:
    """Send ``rows`` to Neo4j in batches and log throughput."""
    started = time.perf_counter()
    for chunk in batches(rows):
        client.write(query, {"rows": chunk})
    elapsed = time.perf_counter() - started
    logger.info(f"{label:<10} {len(rows):>7} rows in {elapsed:5.1f}s")


def seed(client: Neo4jClient, raw_dir: Path = RAW_DIR) -> dict:
    """Run the full ETL and return the cleaning statistics."""
    dataset = load_dataset(raw_dir)
    create_schema(client)
    load_rows(client, "countries", UPSERT_COUNTRIES, dataset["countries"])
    load_rows(client, "airports", UPSERT_AIRPORTS, dataset["airports"])
    load_rows(client, "airlines", UPSERT_AIRLINES, dataset["airlines"])
    load_rows(client, "routes", UPSERT_ROUTES, dataset["routes"])
    load_rows(client, "serves", UPSERT_SERVES, dataset["serves"])
    client.write(COMPUTE_DEGREES)
    return dataset["stats"]


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--if-empty", action="store_true", help="skip when the graph already has airports")
    parser.add_argument("--reset", action="store_true", help="delete every node before loading")
    args = parser.parse_args()

    client = Neo4jClient.from_settings(get_settings())
    try:
        if args.reset:
            client.run(WIPE)
            logger.info("graph wiped")
        if args.if_empty and client.read(COUNT_AIRPORTS)[0]["n"] > 0:
            logger.info("graph already seeded, skipping (use --reset to reload)")
            return
        stats = seed(client)
        logger.info(f"cleaning stats: {json.dumps(stats)}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
