"""The device API's wire format: what the broker sends the renderer, as pydantic models.

These are the contract with the renderer (pi-dashboard's countdown-core). They are separate from the
SQLAlchemy models in models.py on purpose: a column can change (with a migration) without the wire
format changing, and the other way round. A route builds one of these from the database rows, so a
payload that doesn't match fails here, in the broker, not on the device.

`openapi.json` at the repo root is generated from these (see openapi.py); a test fails if it's stale.
The renderer generates its own copy of these models from that file.

Fields have no defaults so the schema marks them all required: a response always carries every field.
Change the format by adding fields; the renderer ignores ones it doesn't know, which is what lets an
older device keep working against a newer broker."""

from typing import Literal

from pydantic import BaseModel

# Bump when the format changes: minor for an added field, major for a removed or reshaped one.
API_VERSION = "1.0.0"


class Screen(BaseModel):
    orientation: Literal["horizontal", "vertical"]
    # In pixels, as the screen reported at registration.
    width: int
    height: int


class Weather(BaseModel):
    api_key: str
    location: str


class NoticeBoard(BaseModel):
    postcode: str | None


class Tfl(BaseModel):
    app_key: str
    # Flat ordered list of NaPTAN stop IDs to cycle through on the display.
    stop_ids: list[str]


class Spotify(BaseModel):
    enabled: bool


class Glowmarkt(BaseModel):
    username: str | None
    password: str | None


class ConfigResponse(BaseModel):
    """GET /api/config."""

    device_id: str
    # Seconds between updates.
    interval: int
    screen: Screen
    weather: Weather
    notice_board: NoticeBoard
    tfl: Tfl
    spotify: Spotify
    glowmarkt: Glowmarkt
    # Null once the device is paired.
    pairing_code: str | None


class Error(BaseModel):
    error: str
