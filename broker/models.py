"""The broker's schema. Change it here, then generate a migration with
`flask --app wsgi db migrate -m "..."` -- the app applies it on next start.

Timestamps are timezone-aware UTC datetimes (see UTCDateTime)."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .crypto import decrypt, encrypt
from .db import UTCDateTime, db


class Device(db.Model):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    # One secret per role (see auth.ROLES), each identifying the device on its own. SHA-256 hex (see auth.hash_secret).
    renderer_secret_hash: Mapped[str | None] = mapped_column(String, index=True, unique=True)
    display_secret_hash: Mapped[str | None] = mapped_column(String, index=True, unique=True)
    # What the device's owner's browser presents to pair with it without a code (see
    # services/pairing.py). Derived from the renderer's secret, so the device never has to send it.
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

    config: Mapped["DeviceConfig | None"] = relationship(back_populates="device", cascade="all, delete-orphan")
    pairing_codes: Mapped[list["PairingCode"]] = relationship(back_populates="device", cascade="all, delete-orphan")
    spotify_token: Mapped["SpotifyToken | None"] = relationship(cascade="all, delete-orphan")
    glowmarkt_credentials: Mapped["GlowmarktCredentials | None"] = relationship(cascade="all, delete-orphan")
    frame: Mapped["Frame | None"] = relationship(cascade="all, delete-orphan")
    assignment: Mapped["RendererAssignment | None"] = relationship(back_populates="device")


class PairingCode(db.Model):
    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    device: Mapped[Device] = relationship(back_populates="pairing_codes")


ORIENTATIONS = ("horizontal", "vertical")


class DeviceConfig(db.Model):
    __tablename__ = "device_config"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    interval: Mapped[int] = mapped_column(Integer, default=15, server_default=text("15"))
    weather_location: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    postcode: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    spotify_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    tfl_stop_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # How the screen is mounted, one of ORIENTATIONS.
    orientation: Mapped[str] = mapped_column(String, default="vertical", server_default=text("'vertical'"))
    # The screen's size in pixels as it reported at registration; 480x800 for screens registered before it did.
    width: Mapped[int] = mapped_column(Integer, server_default=text("480"))
    height: Mapped[int] = mapped_column(Integer, server_default=text("800"))

    device: Mapped[Device] = relationship(back_populates="config")


class SpotifyToken(db.Model):
    __tablename__ = "spotify_tokens"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    refresh_token: Mapped[str] = mapped_column(String)
    access_token: Mapped[str | None] = mapped_column(String)
    # A Unix timestamp, as Spotify's expires_in is counted from "now".
    expires_at: Mapped[float | None] = mapped_column(Float)


class GlowmarktCredentials(db.Model):
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


class Frame(db.Model):
    __tablename__ = "frames"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    frame: Mapped[bytes] = mapped_column(LargeBinary)
    etag: Mapped[str] = mapped_column(String)
    rendered_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PendingRegistration(db.Model):
    """A renderer or screen that registered and is waiting to be matched with the other role
    (see services/registration.py). Deleted once matched."""

    __tablename__ = "pending_registrations"

    secret_hash: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RendererAssignment(db.Model):
    """The renderer a screen is to have, which the orchestrator turns into a running container (see
    services/assignments.py). Created when the screen's owner names it; the slug can't change after."""

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
