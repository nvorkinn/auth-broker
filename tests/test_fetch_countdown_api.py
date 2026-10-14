"""scripts/fetch_countdown_api.py, which downloads the source of the latest countdown-api into src/.

Everything here is local files: a latest.json, and a wheel on a "release" laid out like GitHub's
(releases/tag/<tag> is the page, releases/download/<tag>/<file> the file)."""

import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_countdown_api.py"
spec = importlib.util.spec_from_file_location("fetch_countdown_api", SCRIPT)
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)

VERSION = "9.9.9"
NAME = f"countdown_api-{VERSION}-py3-none-any.whl"


def make_wheel(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def publish(tmp_path: Path, wheel: bytes, *, sha256: str | None = None, **latest) -> Path:
    """A release on disk, and the latest.json that describes it. Returns the latest.json."""
    download = tmp_path / "releases" / "download" / f"v{VERSION}"
    download.mkdir(parents=True, exist_ok=True)
    (download / NAME).write_bytes(wheel)
    document = {
        "version": VERSION,
        "release": str(tmp_path / "releases" / "tag" / f"v{VERSION}"),
        "files": {"SHA256SUMS": "0" * 64, NAME: sha256 or hashlib.sha256(wheel).hexdigest()},
    }
    document.update(latest)
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(document))
    return path


GOOD = {
    "countdown_api/__init__.py": "",
    "countdown_api/models.py": "class ConfigResponse: ...\n",
    "countdown_api/openapi.yaml": "openapi: 3.1.0\n",
    f"countdown_api-{VERSION}.dist-info/METADATA": "Name: countdown-api\n",
}


def run(monkeypatch, latest: Path, out: Path) -> None:
    monkeypatch.setenv("COUNTDOWN_API_LATEST", str(latest))
    fetch.main(out)


def test_it_unpacks_the_package_of_the_latest_wheel(tmp_path, monkeypatch, capsys):
    latest = publish(tmp_path, make_wheel(GOOD))

    run(monkeypatch, latest, tmp_path / "src")

    assert (tmp_path / "src/countdown_api/models.py").read_text() == "class ConfigResponse: ...\n"
    assert (tmp_path / "src/countdown_api/openapi.yaml").is_file()
    assert not list((tmp_path / "src").glob("*.dist-info")), "only the package is unpacked"
    assert "countdown-api 9.9.9: 3 files" in capsys.readouterr().out


def test_a_newer_fetch_replaces_the_old_package(tmp_path, monkeypatch):
    out = tmp_path / "src"
    (out / "countdown_api").mkdir(parents=True)
    (out / "countdown_api/removed_since.py").write_text("old")
    (out / "other_package").mkdir()
    (out / "other_package/keep.py").write_text("untouched")

    run(monkeypatch, publish(tmp_path, make_wheel(GOOD)), out)

    assert not (out / "countdown_api/removed_since.py").exists()
    assert (out / "other_package/keep.py").read_text() == "untouched"


def test_a_wheel_that_doesnt_match_latest_json_is_refused(tmp_path, monkeypatch):
    latest = publish(tmp_path, make_wheel(GOOD), sha256="1" * 64)

    with pytest.raises(SystemExit, match="doesn't match the SHA-256"):
        run(monkeypatch, latest, tmp_path / "src")

    assert not (tmp_path / "src").exists()


def test_a_file_that_would_land_outside_the_package_is_refused(tmp_path, monkeypatch):
    latest = publish(tmp_path, make_wheel({**GOOD, "countdown_api/../evil.py": "boom"}))

    with pytest.raises(SystemExit, match="outside its package"):
        run(monkeypatch, latest, tmp_path / "src")

    assert not (tmp_path / "evil.py").exists()
    assert not (tmp_path / "src").exists()


def test_a_wheel_without_the_package_is_refused(tmp_path, monkeypatch):
    latest = publish(tmp_path, make_wheel({f"countdown_api-{VERSION}.dist-info/METADATA": "x"}))

    with pytest.raises(SystemExit, match="no countdown_api/ package"):
        run(monkeypatch, latest, tmp_path / "src")


def test_latest_json_must_describe_exactly_one_wheel_of_the_version_it_names(tmp_path, monkeypatch):
    no_wheel = publish(tmp_path, make_wheel(GOOD), files={"SHA256SUMS": "0" * 64})
    with pytest.raises(SystemExit, match="Expected one wheel"):
        run(monkeypatch, no_wheel, tmp_path / "src")

    wrong_version = publish(tmp_path, make_wheel(GOOD), version="1.0.0")
    with pytest.raises(SystemExit, match="isn't version 1.0.0"):
        run(monkeypatch, wrong_version, tmp_path / "src")
