"""Manual admin for the broker's SQLite database, for development. Run it inside the container:

docker compose exec auth-broker python -m broker.admin list
docker compose exec auth-broker python -m broker.admin unpair <device_id> [--config]
docker compose exec auth-broker python -m broker.admin forget <device_id>
"""

import argparse
import sqlite3
import sys
from datetime import UTC, datetime

from .db import _db_path

DEVICE_TABLES = ["pairing_codes", "device_config", "spotify_tokens", "glowmarkt_credentials"]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _exists(conn, device_id: str) -> bool:
    return conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone() is not None


def list_devices(conn) -> None:
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        """
        SELECT d.device_id, d.device_name, d.created_at, d.paired_at, c.weather_location, c.tfl_stop_ids,
               (SELECT code FROM pairing_codes p WHERE p.device_id = d.device_id AND p.expires_at > ?) AS code
        FROM devices d LEFT JOIN device_config c ON c.device_id = d.device_id
        ORDER BY d.created_at
        """,
        (now,),
    ).fetchall()
    if not rows:
        print("No devices.")
    for row in rows:
        name = row["device_name"] or "unnamed"
        paired = f"paired {row['paired_at']}" if row["paired_at"] else "NOT paired"
        code = f"  code {row['code']}" if row["code"] else ""
        weather = row["weather_location"] or "-"
        fields = [f"{name} ({row['device_id']})", f"created {row['created_at']}", paired, f"weather={weather}"]
        print("  ".join([*fields, f"stops={row['tfl_stop_ids']}"]) + code)


def unpair(conn, device_id: str, wipe_config: bool) -> None:
    conn.execute("UPDATE devices SET paired_at = NULL WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM pairing_codes WHERE device_id = ?", (device_id,))
    if wipe_config:
        conn.execute(
            "UPDATE device_config SET interval = 15, weather_location = '', postcode = '', spotify_enabled = 0, "
            "tfl_stop_ids = '[]' WHERE device_id = ?",
            (device_id,),
        )
        conn.execute("DELETE FROM spotify_tokens WHERE device_id = ?", (device_id,))
        conn.execute("DELETE FROM glowmarkt_credentials WHERE device_id = ?", (device_id,))
    conn.commit()


def forget(conn, device_id: str) -> None:
    for table in DEVICE_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m broker.admin", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show every device and whether it's paired")
    unpair_parser = commands.add_parser("unpair", help="make a device show a pairing code again")
    unpair_parser.add_argument("device_id")
    unpair_parser.add_argument("--config", action="store_true", help="also reset its settings and linked accounts")
    forget_parser = commands.add_parser("forget", help="delete a device entirely (it must register again)")
    forget_parser.add_argument("device_id")
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        if args.command == "list":
            list_devices(conn)
            return 0
        if not _exists(conn, args.device_id):
            print(f"No device {args.device_id}.", file=sys.stderr)
            return 1
        if args.command == "unpair":
            unpair(conn, args.device_id, args.config)
            print(f"{args.device_id} is unpaired; it will show a new pairing code on its next poll.")
        else:
            forget(conn, args.device_id)
            print(f"{args.device_id} forgotten; delete its credentials file so it registers again.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
