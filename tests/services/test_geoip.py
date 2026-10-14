import gzip
import importlib.util
import io
from datetime import datetime
from pathlib import Path

import pytest

from broker.services import geoip
from broker.services.geoip import GeoData, Place

PLACES = [
    Place("London", 51.50853, -0.12574, 8961989),
    Place("Croydon", 51.38333, -0.1, 192064),
    Place("Reading", 51.45625, -0.97113, 318014),
    Place("Inverness", 57.47908, -4.22398, 47287),
]


class FakeReader:
    """Stands in for DB-IP's database: {ip: record}, raising for what isn't an IP like maxminddb does."""

    def __init__(self, records):
        self.records = records

    def get(self, ip):
        if not ip[0].isdigit():
            raise ValueError(f"{ip} is not a valid IP address")
        return self.records.get(ip)


def _record(lat, lon, city=None):
    record = {"location": {"latitude": lat, "longitude": lon}}
    if city:
        record["city"] = {"names": {"en": city}}
    return record


@pytest.fixture
def geodata(monkeypatch):
    records = {}
    monkeypatch.setattr(geoip, "_load", lambda directory: GeoData(FakeReader(records), PLACES))
    return records


def test_suggests_the_biggest_place_nearby_not_the_nearest(app, geodata):
    geodata["81.2.69.1"] = _record(51.40, -0.11, city="Thornton Heath")  # next to Croydon, in Greater London
    with app.app_context():
        assert geoip.suggest_weather_location("81.2.69.1") == "London"


def test_a_city_of_its_own_is_kept_when_the_bigger_one_is_too_far(app, geodata):
    geodata["81.2.69.2"] = _record(51.45, -0.98, city="Reading")
    with app.app_context():
        assert geoip.suggest_weather_location("81.2.69.2") == "Reading"


def test_falls_back_to_the_databases_own_name_with_no_town_nearby(app, geodata):
    geodata["81.2.69.3"] = _record(58.2, -6.38, city="Stornoway")
    with app.app_context():
        assert geoip.suggest_weather_location("81.2.69.3") == "Stornoway"


@pytest.mark.parametrize("ip", ["10.0.0.1", "not-an-ip", "", None])
def test_no_suggestion_for_an_ip_the_database_cant_place(app, geodata, ip):
    geodata["10.0.0.1"] = {"city": {}}
    with app.app_context():
        assert geoip.suggest_weather_location(ip) is None


def test_no_suggestion_without_the_data(app, tmp_path):
    app.config["GEODATA_DIR"] = str(tmp_path)
    with app.app_context():
        assert geoip.suggest_weather_location("81.2.69.1") is None


def _fetch_geodata():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fetch_geodata.py"
    spec = importlib.util.spec_from_file_location("fetch_geodata", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _geonames_line(name, feature_code, lat="51.5", lon="-0.1", population="100000"):
    fields = [""] * 19
    fields[1], fields[4], fields[5], fields[7], fields[14] = name, lat, lon, feature_code, population
    return "\t".join(fields) + "\n"


def test_fetch_keeps_towns_and_cities_but_not_parts_of_them():
    cities = io.StringIO(
        _geonames_line("London", "PPLC") + _geonames_line("Soho", "PPLX") + _geonames_line("Croydon", "PPLA3")
    )
    places = list(_fetch_geodata().places_from_geonames(cities))
    assert [p[0] for p in places] == ["London", "Croydon"]
    assert places[0] == ("London", "51.5", "-0.1", "100000")


def test_fetch_tries_this_month_then_last():
    fetch = _fetch_geodata()
    assert fetch._months(datetime(2026, 10, 1)) == ["2026-10", "2026-09"]
    assert fetch._months(datetime(2027, 1, 1)) == ["2027-01", "2026-12"]


def test_fetch_dbip_uses_a_browser_user_agent(monkeypatch, tmp_path):
    fetch = _fetch_geodata()
    compressed = gzip.compress(b"database")

    def urlopen(request, timeout):
        assert request.full_url == fetch.DBIP_URL.format(month="2026-10")
        assert request.get_header("User-agent") == "Mozilla/5.0"
        assert timeout == 120
        return io.BytesIO(compressed)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    fetch.fetch_dbip(tmp_path, datetime(2026, 10, 1))

    assert (tmp_path / "dbip-city-lite.mmdb").read_bytes() == b"database"
