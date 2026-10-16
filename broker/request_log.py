"""QA tracing: with LOG_REQUEST_HEADERS on, one JSON log line per request.

The line has the request's method, path, query and every header, and the response's status and Location. It is
what shows where the login proxy sent a browser and what it sent back. Credentials are never logged:
Authorization and Cookie values are replaced.
"""

import json

from flask import Flask, Response, request

REDACTED_HEADERS = {"authorization", "cookie"}


def init_app(app: Flask) -> None:
    """Log every request `app` answers, if LOG_REQUEST_HEADERS is on."""
    if not app.config["LOG_REQUEST_HEADERS"]:
        return

    @app.after_request
    def log_request(response: Response) -> Response:
        app.logger.info(
            "request %s",
            json.dumps(
                {
                    "method": request.method,
                    "path": request.path,
                    "query": request.query_string.decode(errors="replace"),
                    "headers": {
                        name: "REDACTED" if name.lower() in REDACTED_HEADERS else value
                        for name, value in request.headers.items()
                    },
                    "status": response.status_code,
                    "location": response.headers.get("Location"),
                }
            ),
        )
        return response
