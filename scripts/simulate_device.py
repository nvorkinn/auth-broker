"""Stands in for a real Pi during local development: registers a device and
shows its pairing code (as a real Pi would get it from /config), so you can test the /pair -> /device browser flow
without real hardware.

Usage: python scripts/simulate_device.py [base_url]
"""

import secrets
import sys

import requests

base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
device_secret = secrets.token_urlsafe(24)

register = requests.post(f"{base_url}/api/devices/register", json={"device_secret": device_secret})
register.raise_for_status()
device_id = register.json()["device_id"]

config = requests.get(
    f"{base_url}/api/devices/{device_id}/config",
    headers={"Authorization": f"Bearer {device_secret}"},
)
config.raise_for_status()

print(f"device_id:      {device_id}")
print(f"pairing code:   {config.json()['pairing_code']}")
print(f"still needed:   {', '.join(config.json()['setup_missing'])}")
print(f"\nEnter the pairing code at {base_url}/pair")
