"""The broker's schema.

Change it here, then generate a migration with `flask --app wsgi db migrate -m "..."` -- the app applies it on
next start.

Timestamps are timezone-aware UTC datetimes (see UTCDateTime).
"""

from datetime import datetime  # noqa: TC003 -- SQLAlchemy resolves the Mapped[...] annotations at runtime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .crypto import decrypt, encrypt
from .db import UTCDateTime, db


class Device(db.Model):
    """A screen and its renderer, known by their secrets, and who it is paired with."""

    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    # One secret per role (see auth.ROLES), each identifying the device on its own. SHA-256 hex (see auth.hash_secret).
    renderer_secret_hash: Mapped[str | None] = mapped_column(String, index=True, unique=True)
    display_secret_hash: Mapped[str | None] = mapped_column(String, index=True, unique=True)
    # What the device's owner's browser presents to pair with it without a code (see
    # services/pairing.py). Derived from the screen's (display) secret, so the device never has to send it.
    pairing_token: Mapped[str | None] = mapped_column(String, index=True, unique=True)
    # The Authentik user the device is paired with: the user id the login proxy passes in a header
    # (see auth.current_user). One owner; settings are theirs to see and edit.
    owner: Mapped[str | None] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    paired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    device_name: Mapped[str | None] = mapped_column(String)
    # Bumped on authenticated device requests, at most once a minute (see require_device_auth).
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The same for the screen's own requests only. A renderer keeps last_seen_at fresh whether or not the
    # screen is on, so this is what decides when a renderer is suspended (see services/assignments.py).
    display_last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    config: Mapped[DeviceConfig | None] = relationship(back_populates="device", cascade="all, delete-orphan")
    pairing_codes: Mapped[list[PairingCode]] = relationship(back_populates="device", cascade="all, delete-orphan")
    spotify_token: Mapped[SpotifyToken | None] = relationship(cascade="all, delete-orphan")
    glowmarkt_credentials: Mapped[GlowmarktCredentials | None] = relationship(cascade="all, delete-orphan")
    github_token: Mapped[GitHubToken | None] = relationship(cascade="all, delete-orphan")
    apple_music_token: Mapped[AppleMusicToken | None] = relationship(cascade="all, delete-orphan")
    apple_music_plays: Mapped[list[AppleMusicPlay]] = relationship(cascade="all, delete-orphan")
    frame: Mapped[Frame | None] = relationship(cascade="all, delete-orphan")
    assignment: Mapped[RendererAssignment | None] = relationship(back_populates="device")


class PairingCode(db.Model):
    """A code a device shows so its owner can pair with it, until it expires."""

    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    device: Mapped[Device] = relationship(back_populates="pairing_codes")


ORIENTATIONS = ("horizontal", "vertical")
CLOCK_FORMATS = {"24h": "24-hour (13:00)", "12h": "12-hour (1:00 PM)"}
# What the screen is drawn in: the API contract's language, with its name in itself for the config page.
LANGUAGES = {"en": "English", "nb": "Norsk"}
# The screen's refresh interval, in seconds: its default and the range the config site accepts.
DEFAULT_INTERVAL = 20
MIN_INTERVAL, MAX_INTERVAL = 20, 300
# How the screen draws every panel (the departures, Spotify, the notice board): the API contract's style, with the
# name and description the config page shows beside each one's picture (broker/static/styles/<style>.png, cut from
# the renderer's own portrait snapshot of every panel in it).
STYLES = {
    "airy": ("Airy", "Light labels, like the weather"),
    "hero": ("Hero", "Bold headers, with the first item big"),
    "card": ("Card", "Each panel in a rounded card"),
}


# Which service the music panel shows: the API contract's Spotify.source, with its name on the config page.
MUSIC_SOURCES = {"spotify": "Spotify", "apple_music": "Apple Music"}


class DeviceConfig(db.Model):
    """A device's settings, as its owner sets them on the config page."""

    __tablename__ = "device_config"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    interval: Mapped[int] = mapped_column(Integer, default=DEFAULT_INTERVAL, server_default=text("20"))
    clock_format: Mapped[str] = mapped_column(String, default="24h", server_default=text("'24h'"))
    # The screen's language, one of LANGUAGES.
    language: Mapped[str] = mapped_column(String, default="en", server_default=text("'en'"))
    # How every panel is drawn, one of STYLES.
    style: Mapped[str] = mapped_column(String, default="airy", server_default=text("'airy'"))
    weather_location: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    # A UK postcode ("SW1A 1AA") or a Norwegian one ("0150"); see pages.normalize_postcode.
    postcode: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    spotify_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # Which service the music panel (spotify_enabled switches it on) shows, one of MUSIC_SOURCES.
    music_source: Mapped[str] = mapped_column(String, default="spotify", server_default=text("'spotify'"))
    # The developer panel: GitHub pull requests and builds, and AI plans (see routes/developer.py).
    developer_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # Every departure stop, in the owner's order: TfL NaPTAN ids and Entur NSR ids (NSR:...) mixed; the id says
    # which (services/stops.py). Named for when they were all TfL's.
    tfl_stop_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # How the screen is mounted, one of ORIENTATIONS.
    orientation: Mapped[str] = mapped_column(String, default="vertical", server_default=text("'vertical'"))
    # The screen's size in pixels as it reported at registration; 480x800 for screens registered before it did.
    width: Mapped[int] = mapped_column(Integer, server_default=text("480"))
    height: Mapped[int] = mapped_column(Integer, server_default=text("800"))

    device: Mapped[Device] = relationship(back_populates="config")


class SpotifyToken(db.Model):
    """The Spotify account linked to a device: its OAuth tokens."""

    __tablename__ = "spotify_tokens"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    refresh_token: Mapped[str] = mapped_column(String)
    access_token: Mapped[str | None] = mapped_column(String)
    # A Unix timestamp, as Spotify's expires_in is counted from "now".
    expires_at: Mapped[float | None] = mapped_column(Float)


class GlowmarktCredentials(db.Model):
    """The Glowmarkt account linked to a device, its password encrypted at rest."""

    __tablename__ = "glowmarkt_credentials"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    username: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    password_encrypted: Mapped[str | None] = mapped_column(String)

    @property
    def password(self) -> str | None:
        """The saved password, decrypted; None if there isn't one."""
        return decrypt(self.password_encrypted) if self.password_encrypted else None

    @password.setter
    def password(self, plaintext: str) -> None:
        self.password_encrypted = encrypt(plaintext)


class GitHubToken(db.Model):
    """The GitHub token the owner gave for a device's developer panel, encrypted at rest.

    A fine-grained personal access token, read-only. The broker makes the GitHub calls with it, so it never leaves.
    """

    __tablename__ = "github_tokens"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    token_encrypted: Mapped[str] = mapped_column(String)

    @property
    def token(self) -> str:
        """The token, decrypted."""
        return decrypt(self.token_encrypted)

    @token.setter
    def token(self, plaintext: str) -> None:
        self.token_encrypted = encrypt(plaintext)


class AppleMusicToken(db.Model):
    """The Apple Music account linked to a device: its Music User Token, encrypted at rest.

    MusicKit JS gets the token on the config page when the listener signs in to Apple; it lasts about six months and
    can't be refreshed, so it is cleared when Apple refuses it and the page asks them to connect again. The row stays,
    with how far the play log got (see clients/apple_music.record_plays).
    """

    __tablename__ = "apple_music_tokens"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    token_encrypted: Mapped[str | None] = mapped_column(String)
    # The recently played songs' ids, newest first, as last read: new plays are the ones ahead of them.
    seen_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=text("'[]'"))

    @property
    def token(self) -> str | None:
        """The token, decrypted; None once Apple has refused it."""
        return decrypt(self.token_encrypted) if self.token_encrypted else None

    @token.setter
    def token(self, plaintext: str | None) -> None:
        self.token_encrypted = encrypt(plaintext) if plaintext else None


class AppleMusicPlay(db.Model):
    """One play of a song seen in a device's Apple Music history, which the top lists are counted from."""

    __tablename__ = "apple_music_plays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), index=True)
    # When the broker first saw it, which is within a poll of when it was played.
    played_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    song_id: Mapped[str] = mapped_column(String)
    artist: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    # The song as the screen reads a track (see clients/apple_music.as_track).
    track: Mapped[dict] = mapped_column(JSON)


class Frame(db.Model):
    """The latest frame rendered for a device's screen."""

    __tablename__ = "frames"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    frame: Mapped[bytes] = mapped_column(LargeBinary)
    etag: Mapped[str] = mapped_column(String)
    rendered_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RendererAssignment(db.Model):
    """The renderer a screen is to have, which the orchestrator turns into a running container.

    See services/assignments.py. Created when the screen's owner names it; the slug can't change after.
    """

    __tablename__ = "renderer_assignments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # NULL once the device is forgotten, so the orchestrator can still be told to remove the renderer.
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.device_id"), unique=True)
    slug: Mapped[str] = mapped_column(String, unique=True)
    # Handed to the orchestrator, which starts the renderer with it; cleared when the renderer claims it.
    # Stored as is, since the orchestrator needs it back (see the orchestrator's README).
    claim_token: Mapped[str | None] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    detail: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    device: Mapped[Device | None] = relationship(back_populates="assignment")
