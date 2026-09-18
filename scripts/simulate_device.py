"""Stands in for a real Pi during local development: registers a device and
prints a pairing code, so you can test the /pair -> /device browser flow
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

code = requests.post(
    f"{base_url}/api/devices/{device_id}/pairing-code",
    headers={"Authorization": f"Bearer {device_secret}"},
)
code.raise_for_status()

print(f"device_id:      {device_id}")
print(f"device_secret:  {device_secret}")
print(f"pairing code:   {code.json()['code']}  (expires in {code.json()['expires_in_seconds']}s)")
print(f"\nEnter the pairing code at {base_url}/pair")
