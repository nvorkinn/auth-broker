"""The broker's schema.

Change it here, then generate a migration with `flask --app wsgi db migrate -m "..."` -- the app applies it on
next start.

Timestamps are timezone-aware UTC datetimes (see UTCDateTime).
"""

from datetime import datetime  # noqa: TC003 -- SQLAlchemy resolves the Mapped[...] annotations at runtime
from typing import Literal

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .crypto import decrypt, encrypt
from .db import Model, UTCDateTime


class Device(Model):
    """A screen, known by its secret, and who it is paired with."""

    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    # The SHA-256 of the screen's secret (auth.hash_secret), which identifies the device on its own. None once unlinked.
    display_secret_hash: Mapped[str | None] = mapped_column(String, index=True, unique=True)
    # What the owner's browser presents to pair without a code (services/pairing.token_for), derived from the secret.
    pairing_token: Mapped[str | None] = mapped_column(String, index=True, unique=True)
    # The Authentik user id of the device's one owner (auth.current_user).
    owner: Mapped[str | None] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    paired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    device_name: Mapped[str | None] = mapped_column(String)
    # Bumped on authenticated device requests, at most once a minute (auth.LAST_SEEN_RESOLUTION).
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The same for the screen's own requests, which decides whether the render pool still draws it
    # (services/assignments.is_quiet).
    display_last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    config: Mapped[DeviceConfig | None] = relationship(back_populates="device", cascade="all, delete-orphan")
    pairing_codes: Mapped[list[PairingCode]] = relationship(back_populates="device", cascade="all, delete-orphan")
    spotify_token: Mapped[SpotifyToken | None] = relationship(cascade="all, delete-orphan")
    glowmarkt_credentials: Mapped[GlowmarktCredentials | None] = relationship(cascade="all, delete-orphan")
    github_token: Mapped[GitHubToken | None] = relationship(cascade="all, delete-orphan")
    apple_music_token: Mapped[AppleMusicToken | None] = relationship(cascade="all, delete-orphan")
    apple_music_plays: Mapped[list[AppleMusicPlay]] = relationship(cascade="all, delete-orphan")
    assignment: Mapped[RendererAssignment | None] = relationship(back_populates="device", cascade="all, delete-orphan")


class PairingCode(Model):
    """A code a device shows so its owner can pair with it, until it expires."""

    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    device: Mapped[Device] = relationship(back_populates="pairing_codes")


# The values the store contract allows for each setting below, which the columns hold (the config page checks them).
type Orientation = Literal["horizontal", "vertical"]
type ClockFormat = Literal["12h", "24h"]
type Language = Literal["en", "nb"]
type Style = Literal["airy", "hero", "card"]
type WeatherDesign = Literal["columns", "timeline", "overview", "poster"]
type FrameFormat = Literal["mono-1bpp-packed", "png"]
type Layout = Literal[
    "classic", "masthead", "column", "lockscreen", "timeline", "board", "pages", "sidebar", "focus", "newspaper"
]

ORIENTATIONS = ("horizontal", "vertical")
# What a screen may report at registration, not a setting the config page shows.
FRAME_FORMATS: tuple[FrameFormat, ...] = ("mono-1bpp-packed", "png")
CLOCK_FORMATS = {"24h": "24-hour (13:00)", "12h": "12-hour (1:00 PM)"}
# The store contract's languages, each with its name in itself for the config page.
LANGUAGES = {"en": "English", "nb": "Norsk"}
# The store contract's styles, how the screen draws every panel, with the name and description the config page shows
# beside each one's picture (broker/static/styles/<style>.png).
STYLES = {
    "airy": ("Airy", "Light labels, like the weather"),
    "hero": ("Hero", "Bold headers, with the first item big"),
    "card": ("Card", "Each panel in a rounded card"),
}
# The store contract's weather designs, how an HD screen draws the weather (which spans its whole width there), with
# the name and description the config page shows beside each one's picture (broker/static/weather/<design>.png).
WEATHER_DESIGNS = {
    "columns": ("Columns", "Readings in tiles beside the conditions"),
    "timeline": ("Timeline", "The coming twelve hours along the bottom"),
    "overview": ("Overview", "Now, tonight, tomorrow and the weekend at once"),
    "poster": ("Poster", "A very big temperature, readings in a black band"),
}
# The store contract's layouts, which layout a screen is drawn in; each size of screen has its own, and draws one it
# doesn't have as its default. None (the column's default) is the screen's default.
LAYOUTS: tuple[Layout, ...] = (
    "classic",
    "masthead",
    "column",
    "lockscreen",
    "timeline",
    "board",
    "pages",
    "sidebar",
    "focus",
    "newspaper",
)
# The layouts of an HD screen hung portrait, its default first, with the name and description the config page shows
# beside each one's picture (broker/static/layouts/<layout>.png).
HD_PORTRAIT_LAYOUTS: dict[Layout, tuple[str, str]] = {
    "classic": ("Classic", "The weather on top, then the stops, Spotify and the notices"),
    "masthead": ("Masthead", "The clock and the weather in a black band across the top"),
    "column": ("Column", "The weather down a column beside the stops"),
    "lockscreen": ("Lock screen", "A big clock over a card for each panel"),
    "timeline": ("Timeline", "Each departure placed by its minute along a line"),
    "board": ("Route board", "Each route once, with its next three times"),
    "pages": ("Two pages", "The stops and the other panels take turns"),
}
# The layouts of an HD screen hung landscape, likewise, beside broker/static/layouts/landscape-<layout>.png (board,
# column and pages are portrait's ids too, but drawn differently, so their pictures can't share a name).
HD_LANDSCAPE_LAYOUTS: dict[Layout, tuple[str, str]] = {
    "sidebar": ("Sidebar", "The clock and the weather in a black sidebar"),
    "board": ("Departure board", "Both stops as big boards, the weather and energy under them"),
    "column": ("Weather column", "The weather down the left, the stops, then the music on the right"),
    "focus": ("Focus", "Each stop's next departure huge, the rest in a column beside them"),
    "newspaper": ("Newspaper", "A front page in columns under a masthead"),
    "pages": ("Two pages", "Going out and at home, taking turns each minute"),
}
# The HD screen's layouts each way it hangs, the one screen with more than one to pick (layout_choices).
HD_LAYOUTS: dict[Orientation, dict[Layout, tuple[str, str]]] = {
    "vertical": HD_PORTRAIT_LAYOUTS,
    "horizontal": HD_LANDSCAPE_LAYOUTS,
}
# The HD screen's size in pixels, the one screen the weather design and the layout apply to (is_hd_screen).
HD_SCREEN_SIZE = (1872, 1404)


class DeviceConfig(Model):
    """A device's settings, as its owner sets them on the config page."""

    __tablename__ = "device_config"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    clock_format: Mapped[ClockFormat] = mapped_column(String, default="24h", server_default=text("'24h'"))
    # The screen's language, one of LANGUAGES.
    language: Mapped[Language] = mapped_column(String, default="en", server_default=text("'en'"))
    # How every panel is drawn, one of STYLES.
    style: Mapped[Style] = mapped_column(String, default="airy", server_default=text("'airy'"))
    # The weather's town or city by name, as the render pool is sent it: the name of the place below, or, with no
    # place, the text typed (`flask weather backfill` gives it one).
    weather_location: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    # The place picked for the weather, as Open-Meteo's geocoding gave it: its id (GeoNames'), name, what the page
    # shows for it ("London, England, United Kingdom"), ISO country code ("GB") and coordinates. All None for
    # none. See services/weather_place.py.
    weather_place_id: Mapped[int | None] = mapped_column(Integer)
    weather_place_name: Mapped[str | None] = mapped_column(String)
    weather_place_label: Mapped[str | None] = mapped_column(String)
    weather_country_code: Mapped[str | None] = mapped_column(String)
    weather_lat: Mapped[float | None] = mapped_column(Float)
    weather_lon: Mapped[float | None] = mapped_column(Float)
    # How the weather is drawn on an HD screen, one of WEATHER_DESIGNS; other screens ignore it.
    weather_design: Mapped[WeatherDesign] = mapped_column(String, default="columns", server_default=text("'columns'"))
    # Which layout the screen is drawn in, one of LAYOUTS; None for the screen's default (classic on an HD screen hung
    # portrait, sidebar hung landscape).
    layout: Mapped[Layout | None] = mapped_column(String)
    # A UK postcode ("SW1A 1AA") or a Norwegian one ("0150"); see pages.normalize_postcode.
    postcode: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    spotify_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # Apple Music in the Spotify area, beside Spotify or instead of it (see clients/apple_music.py).
    apple_music_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # The developer panel: GitHub pull requests and builds, and AI plans (see broker/providers/developer.py).
    developer_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # Every departure stop, in the owner's order: despite the name, TfL NaPTAN ids and Entur ids (NSR:...) mixed; the
    # id says which (services/stops.py).
    tfl_stop_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # How the screen is mounted, one of ORIENTATIONS.
    orientation: Mapped[Orientation] = mapped_column(String, default="vertical", server_default=text("'vertical'"))
    # The screen's size in pixels as it reported at registration; 480x800 after `devices unpair --config`.
    width: Mapped[int] = mapped_column(Integer, server_default=text("480"))
    height: Mapped[int] = mapped_column(Integer, server_default=text("800"))
    # The format the screen takes its frame in, one of FRAME_FORMATS, as it reported at registration;
    # mono-1bpp-packed (the e-paper's 800x480 1-bit buffer) after `devices unpair --config`.
    format: Mapped[FrameFormat] = mapped_column(String, server_default=text("'mono-1bpp-packed'"))

    device: Mapped[Device] = relationship(back_populates="config")


def is_hd_screen(config: DeviceConfig) -> bool:
    """Whether the config's screen is the HD one (HD_SCREEN_SIZE), whichever way round it reported its size."""
    return (max(config.width, config.height), min(config.width, config.height)) == HD_SCREEN_SIZE


def layout_choices(config: DeviceConfig) -> dict[Layout, tuple[str, str]] | None:
    """Return the layouts the config page offers the config's screen, its default first.

    The way an HD screen hangs has its own (HD_LAYOUTS); every other screen has one layout, so None (no picker).
    """
    return HD_LAYOUTS[config.orientation] if is_hd_screen(config) else None


class SpotifyToken(Model):
    """The Spotify account linked to a device: its OAuth tokens."""

    __tablename__ = "spotify_tokens"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    refresh_token: Mapped[str] = mapped_column(String)
    access_token: Mapped[str | None] = mapped_column(String)
    # A Unix timestamp, as Spotify's expires_in is counted from "now".
    expires_at: Mapped[float | None] = mapped_column(Float)


class GlowmarktCredentials(Model):
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

    def sign_in(self) -> tuple[str, str] | None:
        """Return the username and password to sign in to Glowmarkt with, or None if either is blank.

        The rule for a usable account: the Glowmarkt provider signs in with these, and the config's
        `glowmarkt.enabled` says whether there are any. Raises InvalidToken if the password won't decrypt.
        """
        username = self.username.strip()
        if not username or self.password_encrypted is None:
            return None
        password = self.password or ""
        return (username, password) if password.strip() else None


class GitHubToken(Model):
    """The GitHub token the owner gave for a device's developer panel, encrypted at rest.

    A read-only fine-grained personal access token, used only by the developer provider.
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


class GitHubAppToken(Model):
    """The GitHub dashboard's sign-in through its GitHub App: tokens acting as the dashboard's user, encrypted at rest.

    GitHub issues the access token for 8 hours and the refresh token for 6 months; services/github_app.py renews both
    shortly before the access token expires. One row, for the one dashboard user.
    """

    __tablename__ = "github_app_tokens"

    # The Authentik user id (auth.current_user) who connected.
    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    access_token_encrypted: Mapped[str] = mapped_column(String)
    refresh_token_encrypted: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    refresh_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    @property
    def access_token(self) -> str:
        """The access token, decrypted."""
        return decrypt(self.access_token_encrypted)

    @access_token.setter
    def access_token(self, plaintext: str) -> None:
        self.access_token_encrypted = encrypt(plaintext)

    @property
    def refresh_token(self) -> str:
        """The refresh token, decrypted."""
        return decrypt(self.refresh_token_encrypted)

    @refresh_token.setter
    def refresh_token(self, plaintext: str) -> None:
        self.refresh_token_encrypted = encrypt(plaintext)


class AppleMusicToken(Model):
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


class AppleMusicPlay(Model):
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


class RendererAssignment(Model):
    """A screen's slug, under which the render pool draws it (services/assignments.py).

    Created when the screen is paired; the slug never changes, and goes with the device.
    """

    __tablename__ = "renderer_assignments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.device_id"), unique=True)
    slug: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    device: Mapped[Device | None] = relationship(back_populates="assignment")
