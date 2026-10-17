import re
from fnmatch import fnmatchcase
from pathlib import Path

CADDYFILE = Path(__file__).parent.parent / "caddy" / "Caddyfile"


def _paths_without_login():
    """Return the paths auth.nikolaivorkinn.com's @browser matcher leaves out of the Authentik login."""
    match = re.search(r"@browser \{\s*not path ([^\n]+)", CADDYFILE.read_text())
    assert match is not None
    return match.group(1).split()


def test_device_routes_are_not_behind_the_browser_login(app):
    """A device can't sign in to Authentik: behind forward auth it gets the login page as HTML instead of JSON."""
    paths = _paths_without_login()
    device_routes = [
        rule.rule
        for rule in app.url_map.iter_rules()
        if getattr(app.view_functions[rule.endpoint], "device_auth", False)
    ]
    assert device_routes
    # Caddy's * matches across slashes, as fnmatch's does; a <converter> stands for one segment.
    behind_login = [
        route for route in device_routes if not any(fnmatchcase(re.sub(r"<[^>]+>", "x", route), path) for path in paths)
    ]
    assert behind_login == []
