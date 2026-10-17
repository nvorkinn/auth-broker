"""Look up the ids of existing Authentik objects, for the import blocks in terraform/imports.tf.

Run by Terraform's external data source, so the API token comes from the environment (AUTHENTIK_TOKEN, the
same variable the Authentik provider reads) and never lands in the state or the plan. Read-only: only GETs.
Reads {"url": "<Authentik base url>"} on stdin and prints a JSON object of JSON strings, as the external data
source requires.
"""

import json
import os
import sys
import urllib.request
from urllib.parse import urlparse

TIMEOUT = 30
PAGE_SIZE = 200

# name -> (API path, field that names an object). Every id is a string: the object's pk (a uuid, or an integer
# for providers), which is what the provider's import ids are.
BY_NAME = {
    "flows": ("flows/instances/", "slug"),
    "stages": ("stages/all/", "name"),
    "prompts": ("stages/prompt/prompts/", "name"),
    "policies": ("policies/all/", "name"),
    "providers": ("providers/all/", "name"),
    "applications": ("core/applications/", "slug"),
    "groups": ("core/groups/", "name"),
    "brands": ("core/brands/", "domain"),
    "outposts": ("outposts/instances/", "name"),
    "scope_mappings": ("propertymappings/provider/scope/", "managed"),
}


def get_all(base: str, path: str, token: str) -> list[dict]:
    """Return every object the list endpoint at `path` has, following its pagination."""
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"}:
        msg = f"Not an http(s) URL: {base}"
        raise ValueError(msg)
    results: list[dict] = []
    page = 1
    while page:
        url = f"{base.rstrip('/')}/api/v3/{path}?page={page}&page_size={PAGE_SIZE}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310 -- scheme checked above
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 -- scheme checked above
            body = json.load(response)
        results += body["results"]
        page = body["pagination"]["next"]
    return results


def pk_of(obj: dict) -> str:
    """Return the object's primary key as a string (a brand calls it brand_uuid)."""
    return str(obj["brand_uuid"] if "brand_uuid" in obj else obj["pk"])


def main() -> int:
    """Print the id maps as JSON; return the exit status."""
    token = os.environ.get("AUTHENTIK_TOKEN")
    if not token:
        print("AUTHENTIK_TOKEN is not set.", file=sys.stderr)
        return 1
    base = json.load(sys.stdin)["url"]
    out = {}
    for key, (path, field) in BY_NAME.items():
        out[key] = {str(obj[field]): pk_of(obj) for obj in get_all(base, path, token)}
    # A binding has no name, so it is found by where it sits: "<target pk>/<order>" for a stage in a flow, and
    # "<target pk>/<order>/<policy or group pk>" for a policy on a flow, a stage binding or an application.
    out["flow_bindings"] = {
        f"{b['target']}/{b['order']}": str(b["pk"]) for b in get_all(base, "flows/bindings/", token)
    }
    out["policy_bindings"] = {
        f"{b['target']}/{b['order']}/{b['policy'] or b['group'] or b['user']}": str(b["pk"])
        for b in get_all(base, "policies/bindings/", token)
    }
    json.dump({key: json.dumps(value) for key, value in out.items()}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
