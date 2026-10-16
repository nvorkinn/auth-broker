"""The WSGI entry point gunicorn serves; run directly, it starts Flask's development server."""

import os

from broker import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
