"""Suggests a weather location from the visitor's IP, to prefill the config page's empty field. It's
only ever a suggestion: nothing is stored until the user saves the form.

The lookup is local, so no IP leaves the server: DB-IP's City Lite database places the IP, and the
suggestion is the biggest town or city near there from GeoNames, since an IP's place is rough at best
and the weather is the same across a city anyway. Both files are fetched when the image is built
(scripts/fetch_geodata.py); without them there's simply no suggestion."""

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import maxminddb
from flask import current_app

# How far from where the IP places someone a bigger place can be and still be the suggestion. Inner
# London to central London is under 15 km; Coventry to Birmingham, a city in its own right, is 30.
NEARBY_KM = 25


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    population: int


@dataclass(frozen=True)
class GeoData:
    reader: maxminddb.Reader
    places: list[Place]


@lru_cache
def _load(directory: str) -> GeoData | None:
    path = Path(directory)
    try:
        reader = maxminddb.open_database(str(path / "dbip-city-lite.mmdb"))
        with open(path / "places.tsv", encoding="utf-8") as f:
            places = [
                Place(name, float(lat), float(lon), int(population or 0))
                for name, lat, lon, population in (line.rstrip("\n").split("\t") for line in f)
            ]
    except OSError, ValueError, maxminddb.InvalidDatabaseError:
        current_app.logger.info("No IP location data in %s, so no weather location suggestions", directory)
        return None
    return GeoData(reader, places)


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # Equirectangular: plenty accurate over the few tens of km that matter here.
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


def _biggest_place_near(places: list[Place], latitude: float, longitude: float) -> Place | None:
    max_degrees = NEARBY_KM / 111  # a degree of latitude is ~111 km; skips most places cheaply
    nearby = [
        p
        for p in places
        if abs(p.latitude - latitude) <= max_degrees
        and _distance_km(latitude, longitude, p.latitude, p.longitude) <= NEARBY_KM
    ]
    return max(nearby, key=lambda p: p.population, default=None)


def suggest_weather_location(ip: str | None) -> str | None:
    """A town or city name for where the IP is, or None if there's no telling (a private IP, an IP
    the database doesn't know, or no data)."""
    data = _load(current_app.config["GEODATA_DIR"])
    if data is None or not ip:
        return None
    try:
        record = data.reader.get(ip)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None

    location = record.get("location") or {}
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is not None and longitude is not None:
        place = _biggest_place_near(data.places, latitude, longitude)
        if place is not None:
            return place.name
    # Somewhere with no town of 15,000 people nearby: the database's own name for it will do.
    return (record.get("city") or {}).get("names", {}).get("en") or None
