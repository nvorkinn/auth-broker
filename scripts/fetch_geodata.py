"""Download the data the settings page uses to suggest a weather place from the visitor's IP.

The files land in the given directory and are read by broker/services/geoip.py. Run when the image
is built; standard library only, as it runs before the app's dependencies are installed.

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
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from http.client import HTTPResponse

DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
GEONAMES_URL = "https://download.geonames.org/export/dump/cities15000.zip"

# Parts of a bigger place (PPLX, e.g. a London neighbourhood), and places that are historical,
# abandoned or destroyed: none of them is what someone would call where they live.
SKIPPED_FEATURE_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}


# DB-IP answers 403 to urllib's default "Python-urllib" agent.
USER_AGENT = "countdown-server-build (+https://github.com/nvorkinn/countdown-server)"


def _open(url: str) -> HTTPResponse:
    if not url.startswith("https://"):
        msg = f"Refusing to fetch a non-HTTPS URL: {url}"
        raise ValueError(msg)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 -- scheme checked above
    return urllib.request.urlopen(request, timeout=120)  # noqa: S310 -- scheme checked above


def _months(now: datetime) -> list[str]:
    """Return this month and last: a new month's file can take a day or two to appear."""
    last = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return [f"{now.year}-{now.month:02d}", f"{last[0]}-{last[1]:02d}"]


def fetch_dbip(out: Path, now: datetime | None = None) -> None:
    """Download the newest DB-IP City Lite database into `out`."""
    for month in _months(now or datetime.now(UTC)):
        try:
            with (
                _open(DBIP_URL.format(month=month)) as response,
                gzip.open(response) as mmdb,
                (out / "dbip-city-lite.mmdb").open("wb") as f,
            ):
                shutil.copyfileobj(mmdb, f)
        except urllib.error.HTTPError as e:
            if e.code != HTTPStatus.NOT_FOUND:
                raise
        else:
            print(f"DB-IP City Lite {month}")
            return
    msg = "No DB-IP City Lite file for this month or last"
    raise SystemExit(msg)


def places_from_geonames(cities: Iterator[str]) -> Iterator[tuple[str, str, str, str]]:
    """Yield (name, latitude, longitude, population) for each place in a GeoNames cities file worth naming."""
    for line in cities:
        fields = line.rstrip("\n").split("\t")
        if fields[7] in SKIPPED_FEATURE_CODES:
            continue
        yield fields[1], fields[4], fields[5], fields[14]


def fetch_places(out: Path) -> None:
    """Download GeoNames' cities and write the ones worth naming to `out`/places.tsv."""
    with _open(GEONAMES_URL) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    with archive.open("cities15000.txt") as raw, (out / "places.tsv").open("w", encoding="utf-8") as f:
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
