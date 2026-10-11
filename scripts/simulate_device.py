"""Stands in for a real Pi during local development: registers a device and
shows its pairing code (as a real Pi would get it from /config), so you can test the /pair -> /device browser flow
without real hardware.

With --token it plays a device that has just finished WiFi setup instead: it prints the /pair URL its owner's
browser would be redirected to, and registers only once you've opened it, so you can watch the page wait. Locally
there is no login proxy, so send the user header yourself (e.g. a ModHeader extension: X-Authentik-Uid: me).

Usage: python scripts/simulate_device.py [--token] [base_url]
"""

import hashlib
import secrets
import sys

import requests

args = [arg for arg in sys.argv[1:] if arg != "--token"]
use_token = "--token" in sys.argv[1:]
base_url = args[0] if args else "http://127.0.0.1:5000"
device_secret = secrets.token_urlsafe(24)

if use_token:
    secret_hash = hashlib.sha256(device_secret.encode()).hexdigest()
    token = hashlib.sha256(f"pairing:{secret_hash}".encode()).hexdigest()
    print(f"Open {base_url}/pair#{token}")
    input("then press Enter to bring the device online... ")

register = requests.post(f"{base_url}/api/devices/register", json={"device_secret": device_secret})
register.raise_for_status()
device_id = register.json()["device_id"]

config = requests.get(
    f"{base_url}/api/devices/{device_id}/config",
    headers={"Authorization": f"Bearer {device_secret}"},
)
config.raise_for_status()

print(f"device_id:      {device_id}")
print(f"pairing code:   {config.json()['pairing_code']}")  # None if it was paired by token
print(f"still needed:   {', '.join(config.json()['setup_missing'])}")
if not use_token:
    print(f"\nEnter the pairing code at {base_url}/pair")
