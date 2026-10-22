"""A screen's departure stops, London's and Norway's alike.

Which source each saved id is from, what the settings page shows for it, and the stop search across both.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from flask import current_app

from broker.clients import entur, tfl

SOURCES = ("tfl", "entur")
# The settings page's icon for a stop, by mode: its CSS class (mode-<class>) and symbol. A bus stop (or one of a mode
# not listed) shows its stop letter on a bus-coloured square instead.
MODE_ICONS = {
    "tube": ("tube", "🚇"),
    "metro": ("metro", "T"),
    "tram": ("tram", "🚊"),
    "rail": ("rail", "🚆"),
    "water": ("water", "⛴"),
}


def source_of(stop_id: str) -> Literal["tfl", "entur"]:
    """Entur's ids are NSR:StopPlace:... or NSR:Quay:...; anything else is a TfL NaPTAN id."""
    return "entur" if stop_id.startswith("NSR:") else "tfl"


def resolve(stop_ids: list[str]) -> list[dict[str, Any]]:
    """Return what the settings page shows for each saved stop, in order.

    A stop its source couldn't describe gets a placeholder.
    """
    entur_stops = entur.resolve_stops([s for s in stop_ids if source_of(s) == "entur"])
    tfl_stops = iter(tfl.resolve_stops([s for s in stop_ids if source_of(s) == "tfl"]))
    resolved = []
    for stop_id in stop_ids:
        if source_of(stop_id) == "tfl":
            resolved.append(next(tfl_stops) | {"source": "tfl"})
        else:
            resolved.append(entur_stops.get(stop_id) or _unresolved_entur(stop_id))
    return resolved


def _unresolved_entur(stop_id: str) -> dict[str, Any]:
    return {"id": stop_id, "source": "entur", "name": stop_id, "mode": "unknown", "letter": "", "lines": []}


def _norwegian_first(query: str) -> bool:
    return any(letter in query.lower() for letter in "æøå")


def search(query: str) -> list[dict[str, Any]]:
    """Return stops matching the query from both sources, asked at once.

    A query with æ, ø or å lists Norway's first, any other London's. Raises only if both searches fail.
    """
    # Flask's only way to unwrap the proxy for other threads (SLF001), which Flask types as the app itself (ty).
    app = current_app._get_current_object()  # noqa: SLF001 -- see above  # ty: ignore[unresolved-attribute]

    def run(source: str) -> list[dict[str, Any]]:
        with app.app_context():
            if source == "entur":
                return entur.search_stops(query)
            return [result | {"source": "tfl", "area": "London", "indent": False} for result in tfl.search_stops(query)]

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futures = {source: pool.submit(run, source) for source in SOURCES}
    results: dict[str, list[dict[str, Any]]] = {}
    for source, future in futures.items():
        try:
            results[source] = future.result()
        except Exception:
            current_app.logger.exception("Stop search failed at %s", source)
    if not results:
        msg = "Every stop search failed"
        raise RuntimeError(msg)
    order = ("entur", "tfl") if _norwegian_first(query) else ("tfl", "entur")
    return [result for source in order for result in results.get(source, [])]
