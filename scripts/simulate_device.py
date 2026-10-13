"""Stands in for a real screen (e.g. the ESP32) during development: registers as a display and shows
its pairing code, as the real screen would, so you can test the /pair -> /device browser flow without
the hardware. Works against a local broker or the deployed one.

With --token it plays a screen that has just finished WiFi setup instead: it prints the /pair URL its
owner's browser would be redirected to, and registers only once you've opened it, so you can watch the
page wait.

/pair itself needs a logged-in user. Deployed, that's your Authentik login in the browser; locally
there is no login proxy, so send the user header yourself (e.g. a ModHeader extension:
X-Authentik-Uid: me).

Each run is a new screen with a random secret, unless you pass --secret (any random string of 16+
characters, e.g. from `openssl rand -hex 16`); pass the same one to be that screen again.

Usage: python scripts/simulate_device.py [--token] [--secret SECRET] [base_url]
"""

import argparse
import hashlib
import secrets
import sys

import requests

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:5000")
parser.add_argument("--token", action="store_true", help="pair by the redirect token instead of a code")
parser.add_argument("--secret", help="the screen's secret, to be the same screen across runs")
args = parser.parse_args()
base_url = args.base_url.rstrip("/")
device_secret = args.secret or secrets.token_urlsafe(24)

if args.token:
    secret_hash = hashlib.sha256(device_secret.encode()).hexdigest()
    token = hashlib.sha256(f"pairing:{secret_hash}".encode()).hexdigest()
    print(f"Open {base_url}/pair#{token}")
    input("then press Enter to bring the screen online... ")

register = requests.post(
    f"{base_url}/api/devices/register",
    json={"role": "display", "secret": device_secret, "width": 800, "height": 480},
    timeout=10,
)
if register.status_code in (429, 503):
    sys.exit(f"register: {register.status_code}, try again in {register.headers.get('Retry-After')}s")
register.raise_for_status()

print(f"device_id:      {register.json()['device_id']}")

if args.token:
    print("\nThe /pair page should now pair and move on.")
else:
    code = requests.post(
        f"{base_url}/api/devices/pairing-code", headers={"Authorization": f"Bearer {device_secret}"}, timeout=10
    )
    code.raise_for_status()
    print(f"pairing code:   {code.json()['code']}  (valid {code.json()['expires_in_seconds'] // 60} minutes)")
    print(f"\nEnter the pairing code at {base_url}/pair")
