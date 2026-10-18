"""victoria-metrics/dashboards: vmui's dashboards, in the shape vmui reads, and baked into the image where it looks.

scripts/check_vmui_dashboards.py (CI's image job) checks every query parses, against the image itself.
"""

import json
import re
from pathlib import Path

import pytest

VICTORIA_METRICS = Path(__file__).resolve().parents[1] / "victoria-metrics"
DASHBOARDS = sorted((VICTORIA_METRICS / "dashboards").glob("*.json"))
# vmui's DashboardSettings, DashboardRow and PanelSettings (app/vmui's README, "Predefined dashboards").
DASHBOARD_KEYS = {"title", "rows"}
ROW_KEYS = {"title", "panels"}
PANEL_KEYS = {"expr", "alias", "title", "description", "unit", "showLegend", "width"}


def _panels(path: Path) -> list[dict]:
    return [panel for row in json.loads(path.read_text())["rows"] for panel in row["panels"]]


def test_there_are_dashboards():
    assert [path.stem for path in DASHBOARDS] == ["screens"]


def test_dashboard_titles_are_unique():
    titles = [json.loads(path.read_text())["title"] for path in DASHBOARDS]
    assert len(set(titles)) == len(titles)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_a_dashboard_has_only_what_vmui_reads(path):
    dashboard = json.loads(path.read_text())
    assert dashboard.keys() <= DASHBOARD_KEYS
    assert dashboard["title"]
    assert dashboard["rows"]
    for row in dashboard["rows"]:
        assert row.keys() <= ROW_KEYS
        assert row["panels"]
        for panel in row["panels"]:
            assert panel.keys() <= PANEL_KEYS, panel.get("title")


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_panel_has_queries_an_alias_for_each_and_a_width_vmui_allows(path):
    for panel in _panels(path):
        assert panel["title"]
        assert panel["expr"]
        assert all(isinstance(expr, str) and expr.strip() for expr in panel["expr"])
        assert len(panel.get("alias", panel["expr"])) == len(panel["expr"]), panel["title"]
        assert 1 <= panel.get("width", 12) <= 12


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_query_reads_countdown_series(path):
    """Every query names a countdown.* series (pi-dashboard's otlp_publisher and pool_metrics)."""
    for panel in _panels(path):
        for expr in panel["expr"]:
            assert re.search(r"\bcountdown\.[a-z_.]+", expr), expr


def test_the_image_puts_them_where_victoria_metrics_reads_them():
    dockerfile = (VICTORIA_METRICS / "Dockerfile").read_text()
    path = re.search(r'"-vmui\.customDashboardsPath=(\S+)"', dockerfile)
    assert path is not None
    assert f"COPY dashboards/ {path.group(1)}/" in dockerfile
