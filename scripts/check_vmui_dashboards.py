"""Check the vmui dashboards against a running VictoriaMetrics: it serves every one, and parses every query in them.

    python3 scripts/check_vmui_dashboards.py http://127.0.0.1:8428

The image job in .github/workflows/tests.yml runs this on the victoria-metrics image it has just built, started with
the image's own CMD (so with -vmui.customDashboardsPath). A query VictoriaMetrics can't parse answers 400; one that
finds nothing (as here, with no data) is fine, which is also how a dashboard stays safe to deploy before the series it
reads exist. tests/test_vmui_dashboards.py checks the files' shape without VictoriaMetrics.

Standard library only, as it runs on the CI runner's own Python.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DASHBOARDS = Path(__file__).resolve().parent.parent / "victoria-metrics" / "dashboards"
START_TIMEOUT_S = 30
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url: str) -> dict:
    """GET `url` and return its JSON; an HTTP error's body is returned too, as VictoriaMetrics explains it there."""
    try:
        with DIRECT.open(url, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        return json.load(e)


def wait_until_up(base: str) -> None:
    """Wait for /health to answer; raise SystemExit if it doesn't in time."""
    deadline = time.monotonic() + START_TIMEOUT_S
    while True:
        try:
            with DIRECT.open(f"{base}/health", timeout=2):
                return
        except OSError:
            if time.monotonic() > deadline:
                sys.exit(f"VictoriaMetrics at {base} didn't answer within {START_TIMEOUT_S}s")
            time.sleep(0.5)


def check(base: str) -> list[str]:
    """Return what's wrong with the dashboards as `base` serves them: nothing if all is well."""
    served = get(f"{base}/vmui/custom-dashboards").get("dashboardsSettings") or []
    expected = sorted(json.loads(path.read_text())["title"] for path in DASHBOARDS.glob("*.json"))
    problems = []
    if sorted(dashboard.get("title") for dashboard in served) != expected:
        problems.append(f"served {[d.get('title') for d in served]}, expected {expected}")
    for dashboard in served:
        for row in dashboard["rows"]:
            for panel in row["panels"]:
                for expr in panel["expr"]:
                    answer = get(f"{base}/api/v1/query?{urllib.parse.urlencode({'query': expr})}")
                    if answer.get("status") != "success":
                        problems.append(f"{dashboard['title']} / {panel.get('title')}: {answer.get('error')}")
    return problems


def main() -> None:
    """Check the dashboards at the URL given, and exit non-zero if anything is wrong."""
    base = sys.argv[1].rstrip("/")
    wait_until_up(base)
    problems = check(base)
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        sys.exit(1)
    print("Every dashboard is served and every query parses.")


if __name__ == "__main__":
    main()
