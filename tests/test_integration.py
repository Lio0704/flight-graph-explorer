"""Integration tests against a real, seeded Neo4j (and optionally the running stack).

Skipped automatically when Neo4j is not reachable. Run with the stack up:
    make docker-up && make test-integration

Set API_BASE_URL (e.g. http://localhost) to also check the containers through nginx.
"""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by name

import os

import httpx
import pytest

from app.config import get_settings
from app.database import Neo4jClient
from app.queries import cypher
from app.queries.schema import NODE_LABELS, RELATIONSHIP_TYPES
from app.services.airports import AirportService
from app.services.network import NetworkService
from app.services.routes import RouteService

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db():
    """Real client; skip the module when Neo4j is down or empty."""
    get_settings.cache_clear()
    settings = get_settings()
    client = Neo4jClient(settings.neo4j_uri, (settings.neo4j_user, settings.neo4j_password),
                         settings.neo4j_database, connection_timeout=3)
    if not client.ping():
        pytest.skip("Neo4j not reachable")
    if client.read("MATCH (a:Airport) RETURN count(a) AS n")[0]["n"] == 0:
        pytest.skip("Neo4j is empty: run `make seed`")
    yield client
    client.close()


def test_connectivity_and_counts(db):
    stats = db.read(cypher.NETWORK_STATS)[0]
    assert stats["airports"] > 5000 and stats["routes"] > 30000


def test_schema_constraints_and_indexes_exist(db):
    names = {r["name"] for r in db.read("SHOW INDEXES YIELD name RETURN name")}
    assert {"airport_iata", "airport_search", "airport_location", "airport_continent"} <= names


def test_only_known_labels_and_types(db):
    labels = {r["label"] for r in db.read("CALL db.labels() YIELD label RETURN label")}
    types = {r["relationshipType"] for r in db.read(
        "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")}
    assert labels <= set(NODE_LABELS) and types <= set(RELATIONSHIP_TYPES)


def test_airport_detail(db):
    cdg = AirportService(db).get("CDG")
    assert cdg["country"] == "France" and cdg["departures"] > 100


def test_search_uses_text_index(db):
    page, _ = cypher.airport_search_query(True, False)
    plan = str(db.explain(page, {"q": "paris", "skip": 0, "limit": 5, "continent": None}))
    assert "TextIndex" in plan or "NodeIndexContainsScan" in plan


def test_nearby_paris(db):
    from app.models.schemas import NearbyParams  # pylint: disable=import-outside-toplevel
    codes = [r["iata"] for r in AirportService(db).nearby(NearbyParams(lat=48.85, lon=2.35, radius_km=40))]
    assert "ORY" in codes and "CDG" in codes


def test_reachable_levels_are_increasing(db):
    result = AirportService(db).reachable("CDG", 2)
    assert [lvl["hops"] for lvl in result["levels"]] == [1, 2]
    assert result["levels"][0]["airports"] > 100


def test_fewest_connections(db):
    result = RouteService(db).fewest_connections("LYS", "SYD")
    assert result["itineraries"][0]["path"][0] == "LYS" and result["itineraries"][0]["hops"] <= 3


def test_gds_dijkstra_and_yen(db):
    service = RouteService(db)
    best = service.shortest_distance("CDG", "SYD")["itineraries"][0]
    alternatives = service.alternatives("CDG", "SYD", 3)["itineraries"]
    assert len(alternatives) == 3
    assert alternatives[0]["total_km"] == pytest.approx(best["total_km"], rel=1e-6)
    assert alternatives[0]["total_km"] <= alternatives[1]["total_km"]


def test_components_and_communities(db):
    service = NetworkService(db)
    world = service.components(None)
    assert world["largest_component_size"] > 3000
    assert service.communities(5)[0]["size"] > 100


def test_airline_and_country(db):
    service = NetworkService(db)
    assert service.airline_hubs("AF")[0]["iata"] == "CDG"
    france = service.country_connectivity("France")
    assert 0 < france["domestic_share"] < 1


def test_read_transaction_refuses_writes(db):
    with pytest.raises(Exception):
        db.read("CREATE (:Airport {iata: 'ZZ9'})")


@pytest.mark.skipif(not os.getenv("API_BASE_URL"), reason="API_BASE_URL not set")
def test_containers_healthy():
    base = os.environ["API_BASE_URL"].rstrip("/")
    health = httpx.get(f"{base}/health", timeout=10)
    assert health.status_code == 200 and health.json()["neo4j"] is True
    assert httpx.get(f"{base}/docs", timeout=10).status_code == 200
