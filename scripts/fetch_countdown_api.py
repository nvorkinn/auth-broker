"""Downloads the source of the latest countdown-api (the contract between the broker and the renderer, and the
code generated from it) into a directory, src/ at the repository root unless one is given.

    python scripts/fetch_countdown_api.py [directory]

https://nvorkinn.github.io/countdown-api/latest.json names the latest release's wheel and its SHA-256. The wheel is
downloaded from the release, checked against that, and its countdown_api/ package (the Pydantic models, the Flask
views, the `requests` client and the contract, openapi.yaml) is unpacked into the directory, which has to be on
the import path (pytest.ini does that for the tests, the Dockerfile for the image). Run it after cloning, and in
every build. Stdlib only, as the Dockerfile runs it before the app's requirements are installed.

COUNTDOWN_API_LATEST points it at another latest.json, a URL or a file, e.g. for a release that isn't published."""

import hashlib
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

LATEST = "https://nvorkinn.github.io/countdown-api/latest.json"
PACKAGE = "countdown_api"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src"
USER_AGENT = "auth-broker-build (+https://github.com/nvorkinn/auth-broker)"


def read(location: str) -> bytes:
    """A URL's body, or a file's."""
    if location.startswith(("http://", "https://")):
        request = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    return Path(location).read_bytes()


def latest_wheel(latest: dict) -> tuple[str, str, str]:
    """The (name, SHA-256, location) of the wheel latest.json describes: the one wheel among its files, on the
    release it links to."""
    wheels = [name for name in latest["files"] if name.endswith(".whl")]
    if len(wheels) != 1:
        sys.exit(f"Expected one wheel in latest.json, found {wheels or 'none'}")
    name = wheels[0]
    if latest["version"] not in name:
        sys.exit(f"{name} isn't version {latest['version']}, which latest.json says is the latest")
    release = latest["release"].replace("/releases/tag/", "/releases/download/")
    return name, latest["files"][name], f"{release}/{name}"


def unpack(wheel: bytes, out: Path) -> int:
    """The wheel's countdown_api/ package into `out`, replacing the one there. Returns the number of files."""
    archive = zipfile.ZipFile(io.BytesIO(wheel))
    members = [m for m in archive.namelist() if m.startswith(f"{PACKAGE}/") and not m.endswith("/")]
    members = [m for m in members if "__pycache__" not in m]
    if not members:
        sys.exit(f"The wheel has no {PACKAGE}/ package")
    root = out.resolve()
    targets = {m: (root / m).resolve() for m in members}
    if any(not target.is_relative_to(root / PACKAGE) for target in targets.values()):
        sys.exit("The wheel has a file outside its package; not unpacking it")
    if (root / PACKAGE).exists():
        shutil.rmtree(root / PACKAGE)
    for member, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(member))
    return len(members)


def main(out: Path) -> None:
    latest = json.loads(read(os.environ.get("COUNTDOWN_API_LATEST", LATEST)))
    name, expected, location = latest_wheel(latest)
    wheel = read(location)
    if hashlib.sha256(wheel).hexdigest() != expected:
        sys.exit(f"{name} doesn't match the SHA-256 in latest.json")
    count = unpack(wheel, out)
    print(f"countdown-api {latest['version']}: {count} files in {out / PACKAGE}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
