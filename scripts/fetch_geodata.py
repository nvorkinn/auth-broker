"""Downloads the data the config page uses to suggest a weather location from the visitor's IP (see
broker/services/geoip.py) into the given directory. Run when the image is built, so it's as fresh as
the release; stdlib only, as it runs before the app's requirements are installed.

- dbip-city-lite.mmdb: DB-IP's IP to City Lite database, published monthly (https://db-ip.com, CC BY 4.0).
- places.tsv: GeoNames' towns and cities of over 15,000 people, as name, latitude, longitude and
  population (https://www.geonames.org, CC BY 4.0).
"""

import gzip
import io
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
GEONAMES_URL = "https://download.geonames.org/export/dump/cities15000.zip"

# Parts of a bigger place (PPLX, e.g. a London neighbourhood), and places that are historical,
# abandoned or destroyed: none of them is what someone would call where they live.
SKIPPED_FEATURE_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}


def _months(now: datetime) -> list[str]:
    """This month and last: a new month's file can take a day or two to appear."""
    last = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return [f"{now.year}-{now.month:02d}", f"{last[0]}-{last[1]:02d}"]


def fetch_dbip(out: Path, now: datetime | None = None) -> None:
    for month in _months(now or datetime.now(UTC)):
        try:
            request = urllib.request.Request(DBIP_URL.format(month=month), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=120) as response:
                with gzip.open(response) as mmdb, open(out / "dbip-city-lite.mmdb", "wb") as f:
                    shutil.copyfileobj(mmdb, f)
            print(f"DB-IP City Lite {month}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    raise SystemExit("No DB-IP City Lite file for this month or last")


def places_from_geonames(cities: io.TextIOBase):
    """(name, latitude, longitude, population) for each place in a GeoNames cities file worth naming."""
    for line in cities:
        fields = line.rstrip("\n").split("\t")
        if fields[7] in SKIPPED_FEATURE_CODES:
            continue
        yield fields[1], fields[4], fields[5], fields[14]


def fetch_places(out: Path) -> None:
    with urllib.request.urlopen(GEONAMES_URL, timeout=120) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    with archive.open("cities15000.txt") as raw, open(out / "places.tsv", "w", encoding="utf-8") as f:
        count = 0
        for place in places_from_geonames(io.TextIOWrapper(raw, encoding="utf-8")):
            f.write("\t".join(place) + "\n")
            count += 1
    print(f"GeoNames: {count} places")


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    fetch_dbip(out)
    fetch_places(out)
