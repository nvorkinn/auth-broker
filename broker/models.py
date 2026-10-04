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
    device_secret_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    paired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    device_name: Mapped[str | None] = mapped_column(String)
    # Bumped on authenticated device requests, at most once a minute (see require_device_auth).
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    config: Mapped["DeviceConfig | None"] = relationship(back_populates="device", cascade="all, delete-orphan")
    pairing_codes: Mapped[list["PairingCode"]] = relationship(back_populates="device", cascade="all, delete-orphan")
    spotify_token: Mapped["SpotifyToken | None"] = relationship(cascade="all, delete-orphan")
    glowmarkt_credentials: Mapped["GlowmarktCredentials | None"] = relationship(cascade="all, delete-orphan")
    frame: Mapped["Frame | None"] = relationship(cascade="all, delete-orphan")


class PairingCode(db.Model):
    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    device: Mapped[Device] = relationship(back_populates="pairing_codes")


class DeviceConfig(db.Model):
    __tablename__ = "device_config"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    interval: Mapped[int] = mapped_column(Integer, default=15, server_default=text("15"))
    weather_location: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    postcode: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    spotify_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    tfl_stop_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=text("'[]'"))

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
