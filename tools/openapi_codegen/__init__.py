"""Generate Pydantic models, `requests` clients and Flask blueprints from an OpenAPI 3.x JSON document.

    python -m tools.openapi_codegen openapi.json --out generated/device_api

See README.md ("Generating a client and server from openapi.json")."""

from .generate import generate, stale_files, write_files
from .schema import SpecError

__all__ = ["SpecError", "generate", "stale_files", "write_files"]
