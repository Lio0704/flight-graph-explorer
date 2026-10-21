"""API tests with a fake Neo4j client: routing, validation, error handling."""

from app.queries import cypher
from tests.conftest import neo4j_error

CDG = {"iata": "CDG", "name": "Charles de Gaulle", "city": "Paris", "country": "France", "continent": "Europe",
       "latitude": 49.0, "longitude": 2.5}


def exist(fake_db, *codes):
    """Make AIRPORTS_EXIST return the given codes."""
    fake_db.on(cypher.AIRPORTS_EXIST, [{"iata": c} for c in codes])


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["neo4j"] is True


def test_health_degraded_when_db_down(client, fake_db):
    fake_db.alive = False
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_stats(client, fake_db):
    fake_db.on(cypher.NETWORK_STATS, [{"airports": 6072, "routes": 37041, "airlines": 545, "countries": 246}])
    assert client.get("/api/stats").json()["routes"] == 37041


def test_swagger_and_openapi(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/routes/shortest" in paths and "/api/agent/ask" in paths


def test_airport_detail(client, fake_db):
    fake_db.on(cypher.AIRPORT_DETAIL, [{**CDG, "departures": 240, "arrivals": 238, "top_airlines": [
        {"name": "Air France", "code": "AF", "routes": 150}]}])
    body = client.get("/api/airports/cdg").json()
    assert body["departures"] == 240
    assert fake_db.calls[-1][1] == {"iata": "CDG"}


def test_airport_not_found(client):
    response = client.get("/api/airports/ZZZ")
    assert response.status_code == 404
    assert "ZZZ" in response.json()["detail"]


def test_airport_code_validation(client):
    assert client.get("/api/airports/TOOLONG").status_code == 422


def test_search_uses_text_index_query_and_pagination(client, fake_db):
    page, count = cypher.airport_search_query(True, True)
    fake_db.on(count, [{"total": 42}]).on(page, [CDG])
    response = client.get("/api/airports", params={"search": "Paris", "continent": "Europe", "skip": 20, "limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 42 and body["skip"] == 20 and body["items"][0]["iata"] == "CDG"
    assert fake_db.calls[-1][1]["q"] == "paris"


def test_search_rejects_bad_limit(client):
    assert client.get("/api/airports", params={"limit": 1000}).status_code == 422


def test_nearby(client, fake_db):
    fake_db.on(cypher.AIRPORTS_NEARBY, [{**CDG, "distance_km": 23.4}])
    response = client.get("/api/airports/nearby", params={"lat": 48.85, "lon": 2.35, "radius_km": 50})
    assert response.json()[0]["distance_km"] == 23.4
    assert fake_db.calls[-1][1]["radius_m"] == 50000


def test_central_airports_uses_whitelisted_metric(client, fake_db):
    query = cypher.central_airports_query("pagerank", True)
    fake_db.on(query, [{**CDG, "score": 9.1}])
    response = client.get("/api/airports/central", params={"metric": "pagerank", "continent": "Europe"})
    assert response.json()[0]["score"] == 9.1
    assert client.get("/api/airports/central", params={"metric": "evil"}).status_code == 422


def test_reachable(client, fake_db):
    exist(fake_db, "CDG")
    fake_db.on(cypher.REACHABLE_BY_HOPS[2], [
        {"hops": 1, "airports": 240, "sample": ["JFK"]}, {"hops": 2, "airports": 1500, "sample": ["SYD"]}])
    body = client.get("/api/airports/CDG/reachable", params={"hops": 2}).json()
    assert body["total_reachable"] == 1740
    assert client.get("/api/airports/CDG/reachable", params={"hops": 9}).status_code == 422


def test_shortest_route(client, fake_db):
    exist(fake_db, "LYS", "BKK")
    fake_db.on(cypher.SHORTEST_BY_HOPS, [{"path": ["LYS", "DXB", "BKK"], "hops": 2, "total_km": 9700.0,
                                          "airlines_per_leg": [["EK"], ["EK"]]}])
    body = client.get("/api/routes/shortest", params={"from": "lys", "to": "BKK"}).json()
    assert body["itineraries"][0]["path"] == ["LYS", "DXB", "BKK"]


def test_shortest_route_same_airport(client):
    assert client.get("/api/routes/shortest", params={"from": "CDG", "to": "CDG"}).status_code == 422


def test_shortest_route_unknown_airport(client, fake_db):
    exist(fake_db, "CDG")
    assert client.get("/api/routes/shortest", params={"from": "CDG", "to": "ZZZ"}).status_code == 404


def test_no_route_found(client, fake_db):
    exist(fake_db, "CDG", "XYZ")
    assert client.get("/api/routes/shortest", params={"from": "CDG", "to": "XYZ"}).status_code == 404


def test_shortest_distance_creates_projection(client, fake_db):
    exist(fake_db, "CDG", "SYD")
    fake_db.on(cypher.GDS_GRAPH_EXISTS, [{"exists": False}])
    fake_db.on(cypher.SHORTEST_BY_DISTANCE, [{"path": ["CDG", "SIN", "SYD"], "hops": 2, "total_km": 16950.2}])
    body = client.get("/api/routes/shortest-distance", params={"from": "CDG", "to": "SYD"}).json()
    assert body["algorithm"].startswith("gds")
    assert cypher.GDS_PROJECT_ROUTES in fake_db.queries()


def test_alternatives(client, fake_db):
    exist(fake_db, "CDG", "SYD")
    fake_db.on(cypher.GDS_GRAPH_EXISTS, [{"exists": True}])
    fake_db.on(cypher.K_SHORTEST_BY_DISTANCE, [
        {"path": ["CDG", "SIN", "SYD"], "hops": 2, "total_km": 16950.2},
        {"path": ["CDG", "DXB", "SYD"], "hops": 2, "total_km": 17000.0}])
    body = client.get("/api/routes/alternatives", params={"from": "CDG", "to": "SYD", "k": 2}).json()
    assert len(body["itineraries"]) == 2
    assert cypher.GDS_PROJECT_ROUTES not in fake_db.queries()


def test_gds_missing_returns_503(client, fake_db):
    exist(fake_db, "CDG", "SYD")
    fake_db.on(cypher.GDS_GRAPH_EXISTS, neo4j_error("Neo.ClientError.Procedure.ProcedureNotFound", "no gds"))
    response = client.get("/api/routes/shortest-distance", params={"from": "CDG", "to": "SYD"})
    assert response.status_code == 503
    assert "Graph Data Science" in response.json()["detail"]


def test_suggest_requires_model(client):
    response = client.get("/api/routes/suggest", params={"iata": "CDG"})
    assert response.status_code == 503
    assert "make train" in response.json()["detail"]


def test_suggest_scores_candidates(client, fake_db, trained_model):
    _ = trained_model
    exist(fake_db, "LYS")
    fake_db.on(cypher.ROUTE_CANDIDATES, [
        {"iata": "BKK", "name": "Suvarnabhumi", "city": "Bangkok", "deg_a": 90, "deg_b": 200,
         "common_neighbours": 12, "adamic_adar": 3.1, "distance_km": 9200.0, "same_country": False},
        {"iata": "BOD", "name": "Merignac", "city": "Bordeaux", "deg_a": 90, "deg_b": 60,
         "common_neighbours": 25, "adamic_adar": 6.0, "distance_km": 440.0, "same_country": True}])
    body = client.get("/api/routes/suggest", params={"iata": "LYS"}).json()
    assert [s["iata"] for s in body["suggestions"]] == ["BOD", "BKK"]
    assert body["suggestions"][0]["status"] == "prediction"
    assert "do NOT exist" in body["disclaimer"]


def test_components_world(client, fake_db):
    fake_db.on(cypher.GDS_GRAPH_EXISTS, [{"exists": True}])
    fake_db.on(cypher.GDS_WCC_STREAM, [{"component_id": 0, "size": 3200, "sample": ["CDG"]},
                                       {"component_id": 7, "size": 1, "sample": ["XXX"]}])
    body = client.get("/api/network/components").json()
    assert body["component_count"] == 2 and body["isolated_airports"] == 1


def test_components_continent_drops_temporary_projection(client, fake_db):
    fake_db.on(cypher.GDS_WCC_STREAM, [{"component_id": 0, "size": 600, "sample": ["CDG"]}])
    body = client.get("/api/network/components", params={"continent": "Europe"}).json()
    assert body["scope"] == "Europe"
    queries = fake_db.queries()
    assert cypher.GDS_PROJECT_CONTINENT in queries and queries[-1] == cypher.GDS_DROP


def test_communities(client, fake_db):
    fake_db.on(cypher.COMMUNITIES, [{"community": 3, "size": 500, "top_hubs": ["FRA"],
                                     "by_continent": [{"continent": "Europe", "airports": 480}]}])
    assert client.get("/api/network/communities").json()[0]["top_hubs"] == ["FRA"]


def test_airline_hubs(client, fake_db):
    fake_db.on(cypher.AIRLINE_HUBS, [{"airline": "Air France", "iata": "CDG", "name": "CDG", "city": "Paris",
                                      "routes": 300}])
    assert client.get("/api/airlines/af/hubs").json()[0]["routes"] == 300
    assert fake_db.calls[-1][1]["code"] == "AF"


def test_airline_hubs_unknown(client):
    assert client.get("/api/airlines/QQ/hubs").status_code == 404


def test_airline_overlap(client, fake_db):
    fake_db.on(cypher.AIRLINE_OVERLAP, [{"first_airline": "Air France", "second_airline": "KLM",
                                         "shared_airport_count": 80, "shared_airports": ["CDG"],
                                         "competing_route_count": 12, "competing_routes": ["CDG-AMS"]}])
    assert client.get("/api/airlines/AF/overlap/KL").json()["competing_route_count"] == 12


def test_country_connectivity(client, fake_db):
    fake_db.on(cypher.COUNTRY_EXISTS, [{"name": "France"}])
    fake_db.on(cypher.COUNTRY_CONNECTIVITY, [{"airport_count": 60, "total_routes": 1000, "domestic_routes": 250,
                                              "top_destinations": [{"country": "Spain", "routes": 150}]}])
    body = client.get("/api/countries/France/connectivity").json()
    assert body["domestic_share"] == 0.25


def test_country_without_routes(client, fake_db):
    fake_db.on(cypher.COUNTRY_EXISTS, [{"name": "Atlantis"}])
    assert client.get("/api/countries/Atlantis/connectivity").json()["total_routes"] == 0


def test_country_not_found(client):
    assert client.get("/api/countries/Narnia/connectivity").status_code == 404


def test_database_errors_are_mapped(client, fake_db):
    fake_db.on(cypher.NETWORK_STATS, neo4j_error("Neo.DatabaseError.General.UnknownError", "boom"))
    assert client.get("/api/stats").status_code == 500


def test_agent_requires_api_key(client):
    assert client.post("/api/agent/ask", json={"question": "How many airports?"}).status_code == 401
    assert client.post("/api/agent/ask", json={"question": "x?"}, headers={"X-API-Key": "nope"}).status_code == 401


def test_agent_ask(client, fake_db, auth):
    query = "MATCH (a:Airport) RETURN count(a) AS airports\nLIMIT 50"
    fake_db.on(query, [{"airports": 6072}])
    body = client.post("/api/agent/ask", json={"question": "How many airports exist?"}, headers=auth).json()
    assert body["status"] == "answered"
    assert body["verified_data"]["rows"] == [{"airports": 6072}]
    assert body["grounded"] is True


def test_agent_validate_endpoint(client, auth):
    body = client.post("/api/agent/validate", json={"cypher": "MATCH (n) DETACH DELETE n"}, headers=auth).json()
    assert body["valid"] is False
    assert any(c["check"] == "read_only" and not c["passed"] for c in body["checks"])
    ok = client.post("/api/agent/validate", json={"cypher": "MATCH (a:Airport) RETURN a.iata"}, headers=auth).json()
    assert ok["valid"] is True and ok["query"].endswith("LIMIT 50")


def test_agent_schema(client, auth):
    assert "Airport" in client.get("/api/agent/schema", headers=auth).json()["schema"]
