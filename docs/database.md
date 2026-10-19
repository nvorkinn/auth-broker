# The database

One SQLite file, `data/broker.db` (`/app/data/broker.db` in the container), owned by the broker. The schema is
`broker/models.py`; Alembic migrations (`broker/migrations/`) run under a file lock when the app starts. Timestamps are
UTC. Frames aren't here: the render pool keeps them in Redis.

```mermaid
erDiagram
    devices ||--o| device_config : "settings"
    devices ||--o{ pairing_codes : "codes"
    devices ||--o| renderer_assignments : "slug"
    devices ||--o| spotify_tokens : "Spotify"
    devices ||--o| apple_music_tokens : "Apple Music"
    devices ||--o{ apple_music_plays : "play log"
    devices ||--o| glowmarkt_credentials : "energy"
    devices ||--o| github_tokens : "developer panel"

    devices {
        string device_id PK "token_hex(6)"
        string display_secret_hash UK "SHA-256 of the screen's secret"
        string pairing_token UK
        string owner "authentik uid"
        datetime created_at
        datetime paired_at "NULL until paired"
        string device_name "friendly name"
        datetime last_seen_at
        datetime display_last_seen_at
    }
    device_config {
        string device_id PK, FK
        string clock_format "24h or 12h"
        string language "en or nb"
        string style "airy, hero or card"
        string weather_location "the place's name"
        int weather_place_id "Open-Meteo id"
        string weather_place_name
        string weather_place_label
        string weather_country_code
        float weather_lat
        float weather_lon
        string postcode "UK or Norwegian"
        bool spotify_enabled
        bool apple_music_enabled
        bool developer_enabled
        json tfl_stop_ids "TfL and Entur ids"
        string orientation "vertical or horizontal"
        int width
        int height
    }
    pairing_codes {
        string code PK
        string device_id FK
        datetime expires_at
    }
    renderer_assignments {
        string id PK
        string device_id FK, UK
        string slug UK
        datetime created_at
    }
    spotify_tokens {
        string device_id PK, FK
        string refresh_token
        string access_token
        float expires_at
    }
    apple_music_tokens {
        string device_id PK, FK
        string token_encrypted
        json seen_ids
    }
    apple_music_plays {
        int id PK
        string device_id FK
        datetime played_at
        string song_id
        string artist
        json track
    }
    glowmarkt_credentials {
        string device_id PK, FK
        string username
        string password_encrypted
    }
    github_tokens {
        string device_id PK, FK
        string token_encrypted
    }
```

| Table | One row per | Notes |
|---|---|---|
| `devices` | screen | Created when the screen registers. `owner` is a plain string (there's no users table). `last_seen_at` and `display_last_seen_at` are updated at most once a minute; the latter decides whether the pool still draws the screen. |
| `device_config` | screen | Created at registration with the size the screen reported. `tfl_stop_ids` holds TfL and Entur (`NSR:...`) stops in the owner's order. |
| `pairing_codes` | live code | 10 minutes, one use. Expired codes are swept whenever a new one is issued. |
| `renderer_assignments` | paired screen | The screen's slug (its device id), which names it in `v1:render:screens` and in metrics. It never changes. |
| `spotify_tokens` | linked Spotify account | Plaintext. Refreshed by the music provider. |
| `apple_music_tokens` | linked Apple Music account | The token is encrypted. `seen_ids` is how far the play log has got. |
| `apple_music_plays` | play seen | Apple Music has no top lists, so the music provider logs plays and counts them. |
| `glowmarkt_credentials` | linked Glowmarkt account | The password is encrypted. |
| `github_tokens` | developer panel token | Encrypted. |

- **Encryption.** The encrypted columns use Fernet with `CREDENTIAL_ENCRYPTION_KEY`. The screen secret is stored as an
  unsalted SHA-256, which is enough for a long random string. The pairing token, pairing codes and Spotify tokens are
  in the clear.
- **Deleting.** Every table hangs off `devices`. There's no `ON DELETE` in the schema: the ORM cascades, so
  `flask devices forget` removes the device and all its rows. SQLite enforces the foreign keys.
- **Resetting.** `flask devices unpair --config` replaces `device_config`, so its width and height go back to the column
  defaults (480x800), not the size the screen registered with. Registering again doesn't update them.
- **Read-only readers.** The glow and developer providers open the file with SQLite's `mode=ro` on a read-only mount.
  That works because the database uses the default rollback journal, not WAL (`tests/test_db.py` checks).
- **Rate limits** for `/pair` codes and `/api/devices/register` are in memory, per IP, and reset on restart. They
  assume a single gunicorn worker.
