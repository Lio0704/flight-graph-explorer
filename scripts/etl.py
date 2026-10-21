"""Pure parsing / cleaning functions for the OpenFlights `.dat` files.

No database access here, so everything is unit-testable. `seed_data.py` loads the
output of these functions into Neo4j.

Data quality decisions (see docs/modeling.md):
* Airports without a valid 3-character IATA code are dropped: routes.dat refers to
  airports by IATA, and IATA is our business key.
* Duplicate IATA codes keep the first occurrence.
* Continent is derived from the IANA timezone ("Europe/Paris" -> Europe), with
  explicit overrides for ocean timezones, then filled from the country's majority.
* Routes are aggregated to one edge per directed airport pair (see modeling doc).
* Airlines are kept only if they operate at least one route.
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

NULLS = {"\\N", "", "-", "N/A"}
IATA_RE = re.compile(r"^[A-Z0-9]{3}$")

TZ_PREFIX_TO_CONTINENT = {
    "Europe": "Europe",
    "Asia": "Asia",
    "Africa": "Africa",
    "America": "Americas",
    "Australia": "Oceania",
    "Pacific": "Oceania",
    "Antarctica": "Antarctica",
    "Arctic": "Europe",
}
# Ocean timezones span several continents: decide by country.
ATLANTIC_COUNTRIES = {
    "Iceland": "Europe", "Portugal": "Europe", "Spain": "Europe", "Faroe Islands": "Europe",
    "United Kingdom": "Europe", "Bermuda": "Americas", "Falkland Islands": "Americas",
    "South Georgia and the Islands": "Americas", "Cape Verde": "Africa", "Saint Helena": "Africa",
}
INDIAN_COUNTRIES = {
    "Maldives": "Asia", "British Indian Ocean Territory": "Asia",
    "Christmas Island": "Oceania", "Cocos (Keeling) Islands": "Oceania",
}
PACIFIC_AMERICAS = {"Ecuador", "Chile", "United States"}  # Galapagos, Easter Island, Hawaii -> keep with country


def clean(value: str | None) -> str | None:
    """Strip a raw field and map OpenFlights null markers to None."""
    if value is None:
        return None
    value = value.strip()
    return None if value in NULLS else value


def to_float(value: str | None) -> float | None:
    """Parse a float, returning None for null/invalid values."""
    value = clean(value)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def to_int(value: str | None) -> int | None:
    """Parse an int, returning None for null/invalid values."""
    number = to_float(value)
    return int(number) if number is not None else None


def continent_for(tz_name: str | None, country: str | None) -> str | None:
    """Derive a continent from an IANA timezone name and the country."""
    if not tz_name or "/" not in tz_name:
        return None
    prefix = tz_name.split("/", 1)[0]
    if prefix == "Atlantic":
        return ATLANTIC_COUNTRIES.get(country or "", "Europe")
    if prefix == "Indian":
        return INDIAN_COUNTRIES.get(country or "", "Africa")
    if prefix == "Pacific" and country in PACIFIC_AMERICAS:
        return "Americas"
    return TZ_PREFIX_TO_CONTINENT.get(prefix)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    hav = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return round(2 * radius * math.asin(math.sqrt(hav)), 1)


def read_rows(path: Path) -> list[list[str]]:
    """Read a headerless OpenFlights CSV file."""
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


@dataclass
class CleaningStats:
    """Counters reported by the ETL so data loss is visible and documented."""

    read: int = 0
    kept: int = 0
    dropped: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        """Return a JSON-friendly summary."""
        return {"read": self.read, "kept": self.kept, "dropped": dict(self.dropped)}


def parse_countries(path: Path) -> dict[str, str | None]:
    """Return {country name: ISO code}."""
    return {row[0]: clean(row[1]) for row in read_rows(path) if row and clean(row[0])}


def _airport_from_row(row: list[str]) -> dict:
    name, city, country = clean(row[1]), clean(row[2]), clean(row[3])
    iata = clean(row[4])
    return {
        "openflights_id": to_int(row[0]),
        "name": name,
        "city": city,
        "country": country,
        "iata": iata,
        "icao": clean(row[5]),
        "latitude": to_float(row[6]),
        "longitude": to_float(row[7]),
        "altitude_ft": to_int(row[8]),
        "timezone": clean(row[11]),
        "type": clean(row[12]),
        "continent": continent_for(clean(row[11]), country),
        "search_text": " ".join(p for p in (iata, name, city) if p).lower(),
    }


def parse_airports(path: Path) -> tuple[list[dict], CleaningStats]:
    """Parse and clean airports.dat."""
    stats = CleaningStats()
    airports: dict[str, dict] = {}
    for row in read_rows(path):
        stats.read += 1
        if len(row) < 13:
            stats.dropped["malformed_row"] += 1
            continue
        airport = _airport_from_row(row)
        if not airport["iata"] or not IATA_RE.match(airport["iata"]):
            stats.dropped["no_valid_iata"] += 1
        elif airport["latitude"] is None or airport["longitude"] is None:
            stats.dropped["no_coordinates"] += 1
        elif airport["iata"] in airports:
            stats.dropped["duplicate_iata"] += 1
        else:
            airports[airport["iata"]] = airport
    fill_missing_continents(list(airports.values()))
    stats.kept = len(airports)
    return list(airports.values()), stats


def fill_missing_continents(airports: list[dict]) -> int:
    """Fill unknown continents with the most common continent of the same country."""
    by_country: dict[str, Counter] = defaultdict(Counter)
    for airport in airports:
        if airport["continent"] and airport["country"]:
            by_country[airport["country"]][airport["continent"]] += 1
    filled = 0
    for airport in airports:
        if not airport["continent"] and airport["country"] in by_country:
            airport["continent"] = by_country[airport["country"]].most_common(1)[0][0]
            filled += 1
    return filled


def parse_airlines(path: Path) -> dict[int, dict]:
    """Parse airlines.dat into {openflights_id: airline}."""
    airlines = {}
    for row in read_rows(path):
        if len(row) < 8:
            continue
        airline_id = to_int(row[0])
        if airline_id is None or airline_id <= 0:
            continue
        airlines[airline_id] = {
            "openflights_id": airline_id,
            "name": clean(row[1]),
            "iata": clean(row[3]),
            "icao": clean(row[4]),
            "callsign": clean(row[5]),
            "country": clean(row[6]),
            "active": clean(row[7]) == "Y",
        }
    return airlines


@dataclass
class RouteAggregate:
    """All airline-level routes between one directed airport pair."""

    airlines: set = field(default_factory=set)
    codeshares: int = 0
    max_stops: int = 0
    equipment: set = field(default_factory=set)


def parse_routes(path: Path, airports: dict[str, dict], airlines: dict[int, dict]) -> tuple[list, list, CleaningStats]:
    """Aggregate routes.dat into ROUTE rows and Airline-SERVES-Airport rows."""
    stats = CleaningStats()
    pairs: dict[tuple[str, str], RouteAggregate] = defaultdict(RouteAggregate)
    serves: Counter = Counter()
    for row in read_rows(path):
        stats.read += 1
        if len(row) < 9:
            stats.dropped["malformed_row"] += 1
            continue
        src, dst = clean(row[2]), clean(row[4])
        if src not in airports or dst not in airports:
            stats.dropped["unknown_airport"] += 1
            continue
        if src == dst:
            stats.dropped["self_loop"] += 1
            continue
        stats.kept += 1
        agg = pairs[(src, dst)]
        if clean(row[0]):
            agg.airlines.add(clean(row[0]))
        agg.codeshares += clean(row[6]) == "Y"
        agg.max_stops = max(agg.max_stops, to_int(row[7]) or 0)
        agg.equipment.update((clean(row[8]) or "").split())
        airline_id = to_int(row[1])
        if airline_id in airlines:
            serves[(airline_id, src)] += 1
            serves[(airline_id, dst)] += 1

    route_rows = []
    for (src, dst), agg in pairs.items():
        a, b = airports[src], airports[dst]
        route_rows.append({
            "src": src,
            "dst": dst,
            "distance_km": haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]),
            "airlines": sorted(agg.airlines),
            "airline_count": len(agg.airlines),
            "codeshare_count": agg.codeshares,
            "max_stops": agg.max_stops,
            "equipment": sorted(agg.equipment)[:20],
        })
    serves_rows = [{"airline_id": aid, "iata": iata, "route_count": n} for (aid, iata), n in serves.items()]
    return route_rows, serves_rows, stats


def load_dataset(raw_dir: Path) -> dict:
    """Parse all files and return everything `seed_data` needs, plus cleaning stats."""
    countries = parse_countries(raw_dir / "countries.dat")
    airports, airport_stats = parse_airports(raw_dir / "airports.dat")
    airport_index = {a["iata"]: a for a in airports}
    all_airlines = parse_airlines(raw_dir / "airlines.dat")
    routes, serves, route_stats = parse_routes(raw_dir / "routes.dat", airport_index, all_airlines)
    used_airlines = {row["airline_id"] for row in serves}
    airlines = [all_airlines[i] for i in sorted(used_airlines)]

    country_names = {a["country"] for a in airports if a["country"]} | {
        l["country"] for l in airlines if l["country"]
    }
    country_rows = [{"name": n, "iso_code": countries.get(n)} for n in sorted(country_names)]
    return {
        "countries": country_rows,
        "airports": airports,
        "airlines": airlines,
        "routes": routes,
        "serves": serves,
        "stats": {"airports": airport_stats.as_dict(), "routes": route_stats.as_dict()},
    }
