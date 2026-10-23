import struct
import zlib

import fakeredis

from broker.db import db
from broker.models import DeviceConfig
from broker.services import frame_image
from broker.services.frame_record import FRAME_BYTES


def _frame_with_black_pixel(x: int, y: int) -> bytes:
    """Return a white frame in the panel's own layout (800x480, set bit = black) with one black pixel."""
    frame = bytearray(FRAME_BYTES)
    frame[(y * 800 + x) // 8] |= 0x80 >> (x % 8)
    return bytes(frame)


def _decode(png: bytes) -> tuple[int, int, list[bytes]]:
    """Return (width, height, rows) of an 8-bit greyscale PNG with unfiltered rows, as to_png writes them."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    pos, chunks = 8, {}
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        assert struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])[0] == zlib.crc32(kind + data)
        chunks[kind] = chunks.get(kind, b"") + data
        pos += 12 + length
    width, height, depth, colour, *_ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, colour) == (8, 0)
    raw = zlib.decompress(chunks[b"IDAT"])
    rows = [raw[r * (width + 1) : (r + 1) * (width + 1)] for r in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, [row[1:] for row in rows]


def _black_pixels(png: bytes) -> tuple[tuple[int, int], list[tuple[int, int]]]:
    width, height, rows = _decode(png)
    return (width, height), [(x, y) for y, row in enumerate(rows) for x, value in enumerate(row) if value == 0]


def test_a_landscape_frame_is_drawn_as_is():
    size, black = _black_pixels(frame_image.to_png(_frame_with_black_pixel(10, 3), portrait=False))

    assert size == (800, 480)
    assert black == [(10, 3)]


def test_a_portrait_frame_is_turned_back_a_quarter_clockwise():
    # The render turned the portrait picture a quarter anticlockwise: portrait (x, y) is at landscape (y, 479 - x).
    portrait_x, portrait_y = 10, 3
    frame = _frame_with_black_pixel(portrait_y, 479 - portrait_x)
    size, black = _black_pixels(frame_image.to_png(frame, portrait=True))

    assert size == (480, 800)
    assert black == [(portrait_x, portrait_y)]


PNG_FRAME = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def _make_png_screen(app, device_id, width, height, orientation="vertical"):
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        config.width, config.height, config.format, config.orientation = width, height, "png", orientation
        db.session.commit()


def test_a_png_frame_is_served_as_it_is_in_portrait_or_landscape():
    # The page turns a portrait one back (the frame-portrait style); the broker never decodes it.
    assert frame_image.preview(PNG_FRAME, "png", portrait=True) == PNG_FRAME
    assert frame_image.preview(PNG_FRAME, "png", portrait=False) == PNG_FRAME


def test_a_mono_frame_is_previewed_as_before():
    frame = _frame_with_black_pixel(10, 3)

    assert frame_image.preview(frame, "mono-1bpp-packed", portrait=True) == frame_image.to_png(frame, portrait=True)


def test_a_portrait_png_screen_turns_its_picture_on_the_page(app, paired_client, draw):
    client, device_id, _secret = paired_client
    _make_png_screen(app, device_id, 1872, 1404)
    draw(device_id, PNG_FRAME)

    screen = client.get(f"/device/{device_id}/screen").get_data(as_text=True)
    settings = client.get(f"/device/{device_id}").get_data(as_text=True)
    picture = client.get(f"/device/{device_id}/screen/frame.png")

    for html in (screen, settings):
        assert 'class="frame frame-portrait" style="--frame-w: 1404; --frame-h: 1872"' in html
        assert 'width="1872" height="1404"' in html  # the picture as it comes: landscape
    assert picture.status_code == 200
    assert picture.data == PNG_FRAME


def test_a_landscape_png_screen_shows_its_picture_as_it_is(app, paired_client, draw):
    client, device_id, _secret = paired_client
    _make_png_screen(app, device_id, 1872, 1404, orientation="horizontal")
    draw(device_id, PNG_FRAME)

    html = client.get(f"/device/{device_id}/screen").get_data(as_text=True)

    assert 'class="frame" style="--frame-w: 1872; --frame-h: 1404"' in html
    assert 'class="frame frame-portrait"' not in html


def test_a_mono_screen_is_never_turned_on_the_page(paired_client, draw):
    client, device_id, _secret = paired_client
    draw(device_id, bytes(FRAME_BYTES))

    html = client.get(f"/device/{device_id}/screen").get_data(as_text=True)

    assert 'class="frame" style="--frame-w: 480; --frame-h: 800"' in html


def test_screen_page_shows_the_picture_and_polls_at_the_screens_interval(app, paired_client, draw):
    client, device_id, _secret = paired_client
    draw(device_id, bytes(FRAME_BYTES))

    response = client.get(f"/device/{device_id}/screen")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'src="/device/{device_id}/screen/frame.png"' in html
    assert "const intervalMs = 30 * 1000;" in html  # twice a render, as the screen polls
    assert "checked every 30 seconds as the screen does" in html
    assert 'width="480" height="800"' in html


def test_screen_page_polls_as_fast_as_the_screen_while_a_provider_asks(app, paired_client, draw):
    client, device_id, _secret = paired_client
    draw(device_id, bytes(FRAME_BYTES))
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).developer_enabled = True
        db.session.commit()
    app.extensions["redis"] = store = fakeredis.FakeRedis(decode_responses=True)
    store.set(f"v1:pace:developer:{device_id}", "20", ex=60)

    html = client.get(f"/device/{device_id}/screen").get_data(as_text=True)

    assert "const intervalMs = 10 * 1000;" in html


def test_screen_page_before_the_first_frame_says_so(paired_client):
    client, device_id, _ = paired_client

    html = client.get(f"/device/{device_id}/screen").get_data(as_text=True)

    assert "Nothing drawn yet" in html
    assert client.get(f"/device/{device_id}/screen/frame.png").status_code == 404


def test_settings_page_links_to_the_screen(paired_client):
    client, device_id, _ = paired_client

    html = client.get(f"/device/{device_id}").get_data(as_text=True)

    assert f'href="/device/{device_id}/screen"' in html


def test_settings_page_header_shows_the_screens_picture(paired_client, draw):
    client, device_id, _ = paired_client
    draw(device_id, bytes(FRAME_BYTES))

    html = client.get(f"/device/{device_id}").get_data(as_text=True)

    picture = f'<img class="frame-picture" src="/device/{device_id}/screen/frame.png" alt="" width="480" height="800">'
    assert picture in html
    assert "No picture yet" not in html


def test_settings_page_header_before_the_first_frame_says_so(paired_client):
    client, device_id, _ = paired_client

    html = client.get(f"/device/{device_id}").get_data(as_text=True)

    assert "No picture yet" in html
    assert "/screen/frame.png" not in html


def test_settings_page_header_lists_the_saved_settings(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        config.style, config.language, config.clock_format = "card", "nb", "12h"
        db.session.commit()

    html = client.get(f"/device/{device_id}").get_data(as_text=True)

    for chip in ("Card", "Norsk", "0 stops", "12-hour"):
        assert f"<li>{chip}</li>" in html


def test_frame_png_is_the_frame_in_portrait_and_conditional(paired_client, draw):
    client, device_id, _secret = paired_client
    draw(device_id, _frame_with_black_pixel(3, 479 - 10))

    response = client.get(f"/device/{device_id}/screen/frame.png")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, no-cache"
    assert "X-Rendered-At" in response.headers
    assert _black_pixels(response.data) == ((480, 800), [(10, 3)])

    again = client.get(f"/device/{device_id}/screen/frame.png", headers={"If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304
    assert again.data == b""
    assert "X-Rendered-At" in again.headers


def test_frame_png_follows_a_landscape_screen(app, paired_client, draw):
    client, device_id, _secret = paired_client
    draw(device_id, _frame_with_black_pixel(10, 3))
    portrait = client.get(f"/device/{device_id}/screen/frame.png")
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()

    response = client.get(f"/device/{device_id}/screen/frame.png", headers={"If-None-Match": portrait.headers["ETag"]})

    assert response.status_code == 200
    assert _black_pixels(response.data) == ((800, 480), [(10, 3)])


def test_the_screen_is_only_its_owners(app, paired_client, anonymous_client, draw):
    _client, device_id, _secret = paired_client
    draw(device_id, bytes(FRAME_BYTES))
    stranger = app.test_client()
    stranger.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"

    for path in (f"/device/{device_id}/screen", f"/device/{device_id}/screen/frame.png"):
        assert stranger.get(path).status_code == 404
        assert anonymous_client.get(path).status_code == 401
