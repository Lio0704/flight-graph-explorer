"""Tests for the OpenFlights cleaning functions."""

from pathlib import Path

import pytest

from scripts.etl import (
    clean,
    continent_for,
    fill_missing_continents,
    haversine_km,
    load_dataset,
    parse_airlines,
    parse_airports,
    parse_routes,
    to_int,
)

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def write(tmp_path, name, text):
    """Write a small fixture file."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_clean_nulls():
    assert clean("\\N") is None and clean(" - ") is None and clean(" CDG ") == "CDG"
    assert to_int("12.0") == 12 and to_int("abc") is None


@pytest.mark.parametrize("tz, country, expected", [
    ("Europe/Paris", "France", "Europe"),
    ("America/New_York", "United States", "Americas"),
    ("Pacific/Honolulu", "United States", "Americas"),
    ("Pacific/Fiji", "Fiji", "Oceania"),
    ("Atlantic/Reykjavik", "Iceland", "Europe"),
    ("Atlantic/Bermuda", "Bermuda", "Americas"),
    ("Indian/Maldives", "Maldives", "Asia"),
    ("Indian/Mauritius", "Mauritius", "Africa"),
    (None, "France", None),
])
def test_continent_for(tz, country, expected):
    assert continent_for(tz, country) == expected


def test_haversine_paris_new_york():
    assert 5800 < haversine_km(49.0, 2.55, 40.64, -73.78) < 5900


def test_fill_missing_continents():
    airports = [{"country": "France", "continent": "Europe"}, {"country": "France", "continent": None}]
    assert fill_missing_continents(airports) == 1
    assert airports[1]["continent"] == "Europe"


def test_parse_small_files(tmp_path):
    airports_file = write(tmp_path, "airports.dat", "\n".join([
        '1,"A","Paris","France","AAA","LFAA",49,2,100,1,"E","Europe/Paris","airport","x"',
        '2,"B","Lyon","France","BBB","LFBB",45,5,100,1,"E","Europe/Paris","airport","x"',
        '3,"NoCode","X","France",\\N,"LFCC",45,5,100,1,"E","Europe/Paris","airport","x"',
        '4,"Dup","Y","France","AAA","LFDD",45,5,100,1,"E","Europe/Paris","airport","x"',
    ]))
    airports, stats = parse_airports(airports_file)
    assert [a["iata"] for a in airports] == ["AAA", "BBB"]
    assert stats.dropped == {"no_valid_iata": 1, "duplicate_iata": 1}
    assert airports[0]["search_text"] == "aaa a paris"

    airlines = parse_airlines(write(tmp_path, "airlines.dat", '10,"Air X",\\N,"XX","XXX","XCALL","France","Y"\n'))
    routes_file = write(tmp_path, "routes.dat", "\n".join([
        "XX,10,AAA,1,BBB,2,,0,320", "YY,11,AAA,1,BBB,2,Y,0,321 320", "XX,10,BBB,2,AAA,1,,0,320",
        "XX,10,AAA,1,ZZZ,9,,0,320", "XX,10,AAA,1,AAA,1,,0,320",
    ]))
    index = {a["iata"]: a for a in airports}
    routes, serves, route_stats = parse_routes(routes_file, index, airlines)
    by_pair = {(r["src"], r["dst"]): r for r in routes}
    assert by_pair[("AAA", "BBB")]["airlines"] == ["XX", "YY"]
    assert by_pair[("AAA", "BBB")]["codeshare_count"] == 1
    assert by_pair[("AAA", "BBB")]["equipment"] == ["320", "321"]
    assert route_stats.dropped == {"unknown_airport": 1, "self_loop": 1}
    assert {(s["iata"], s["route_count"]) for s in serves} == {("AAA", 2), ("BBB", 2)}


@pytest.mark.skipif(not (RAW / "routes.dat").exists(), reason="dataset not downloaded")
def test_real_dataset_sanity():
    data = load_dataset(RAW)
    assert len(data["airports"]) > 5000
    assert len(data["routes"]) > 30000
    assert all(a["continent"] for a in data["airports"])
    iatas = {a["iata"] for a in data["airports"]}
    assert all(r["src"] in iatas and r["dst"] in iatas for r in data["routes"])
