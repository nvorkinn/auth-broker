"""The dashboards in grafana/dashboards are JSON written for the provisioned VictoriaLogs datasource."""

import json
import re
from pathlib import Path

import pytest

GRAFANA = Path(__file__).resolve().parents[1] / "grafana"
DASHBOARDS = sorted((GRAFANA / "dashboards").glob("*.json"))
QUERY_TYPES = {"instant", "stats", "statsRange", "hits"}


def _datasource() -> dict:
    """type and uid from the provisioning file, which is flat enough to read without a YAML parser."""
    text = (GRAFANA / "provisioning/datasources/victorialogs.yaml").read_text()
    return {key: re.search(rf"^\s+{key}: (\S+)$", text, re.M).group(1) for key in ("type", "uid")}


def test_there_are_dashboards():
    assert [p.stem for p in DASHBOARDS] == ["config-vs-renderers", "platform", "screens"]


def test_dashboard_uids_are_unique():
    uids = [json.loads(p.read_text())["uid"] for p in DASHBOARDS]
    assert len(set(uids)) == len(uids)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_query_uses_the_provisioned_datasource_and_a_known_query_type(path):
    datasource = _datasource()
    panels = [p for p in json.loads(path.read_text())["panels"] if p["type"] not in {"row", "text"}]
    assert panels
    for panel in panels:
        assert panel["datasource"] == {"type": datasource["type"], "uid": datasource["uid"]}
        for target in panel["targets"]:
            assert target["datasource"] == panel["datasource"]
            assert target["queryType"] in QUERY_TYPES
            assert target["expr"].strip()


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_variable_used_in_a_query_is_defined(path):
    dashboard = json.loads(path.read_text())
    defined = {v["name"] for v in dashboard["templating"]["list"]}
    used = set(re.findall(r"\$\{(\w+)\}", json.dumps(dashboard["panels"])))
    assert used <= defined


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_panel_ids_are_unique(path):
    ids = [p["id"] for p in json.loads(path.read_text())["panels"]]
    assert len(set(ids)) == len(ids)


def test_the_dashboard_provider_reads_where_the_image_puts_them():
    path = re.search(r"^\s+path: (\S+)$", (GRAFANA / "provisioning/dashboards/dashboards.yaml").read_text(), re.M)
    assert f"COPY dashboards/ {path.group(1)}/" in (GRAFANA / "Dockerfile").read_text()
