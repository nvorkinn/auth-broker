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


def test_dashboard_and_its_assets_are_behind_authentik(app):
    paths = _paths_without_login()
    routes = [rule.rule for rule in app.url_map.iter_rules() if rule.endpoint.startswith("github_dashboard.")]
    routes += ["/static/github_dashboard.js", "/static/github_dashboard.css"]
    assert routes
    assert not any(fnmatchcase(route, pattern) for route in routes for pattern in paths)


def test_frame_answers_close_the_connection():
    """A 304 through Caddy has no Content-Length, and the ESP32 then reads until the connection closes.

    Without `Connection: close` that's Caddy's idle timeout, 5 minutes, after every unchanged frame.
    """
    site = CADDYFILE.read_text().split("auth.nikolaivorkinn.com {", 1)[1].split("\n}\n", 1)[0]
    assert re.search(r"^\theader /api/frame Connection close$", site, re.MULTILINE)


def test_bare_domain_redirects_to_the_settings_site():
    """nikolaivorkinn.com and www. have nothing of their own; they send visitors on, keeping the path."""
    site = CADDYFILE.read_text().split("nikolaivorkinn.com, www.nikolaivorkinn.com {", 1)[1].split("\n}\n", 1)[0]
    assert site.strip() == "redir https://auth.nikolaivorkinn.com{uri}"
