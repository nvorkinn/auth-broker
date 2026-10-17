import struct
import zlib

from broker.db import db
from broker.models import DeviceConfig
from broker.routes.frames import FRAME_BYTES
from broker.services import frame_image


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
    size, black = _black_pixels(frame_image.to_png(_frame_with_black_pixel(10, 3), upright=False))

    assert size == (800, 480)
    assert black == [(10, 3)]


def test_an_upright_frame_is_turned_back_to_portrait():
    # The renderer turned the portrait picture a quarter anticlockwise: portrait (x, y) is at landscape (y, 479 - x).
    portrait_x, portrait_y = 10, 3
    size, black = _black_pixels(frame_image.to_png(_frame_with_black_pixel(portrait_y, 479 - portrait_x), upright=True))

    assert size == (480, 800)
    assert black == [(portrait_x, portrait_y)]


def _upload(client, secret, frame):
    response = client.put("/api/frame", data=frame, headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 204


def test_screen_page_shows_the_picture_and_polls_at_the_screens_interval(app, paired_client):
    client, device_id, secret = paired_client
    _upload(client, secret, bytes(FRAME_BYTES))

    response = client.get(f"/device/{device_id}/screen")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'src="/device/{device_id}/screen/frame.png"' in html
    assert "const intervalMs = 60 * 1000;" in html
    assert 'width="480" height="800"' in html


def test_screen_page_before_the_first_frame_says_so(paired_client):
    client, device_id, _ = paired_client

    html = client.get(f"/device/{device_id}/screen").get_data(as_text=True)

    assert "Nothing drawn yet" in html
    assert client.get(f"/device/{device_id}/screen/frame.png").status_code == 404


def test_settings_page_links_to_the_screen(paired_client):
    client, device_id, _ = paired_client

    html = client.get(f"/device/{device_id}").get_data(as_text=True)

    assert f'href="/device/{device_id}/screen"' in html


def test_frame_png_is_the_frame_upright_and_conditional(paired_client):
    client, device_id, secret = paired_client
    _upload(client, secret, _frame_with_black_pixel(3, 479 - 10))

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


def test_frame_png_follows_a_horizontal_screen(app, paired_client):
    client, device_id, secret = paired_client
    _upload(client, secret, _frame_with_black_pixel(10, 3))
    upright = client.get(f"/device/{device_id}/screen/frame.png")
    with app.app_context():
        db.session.get(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()

    response = client.get(f"/device/{device_id}/screen/frame.png", headers={"If-None-Match": upright.headers["ETag"]})

    assert response.status_code == 200
    assert _black_pixels(response.data) == ((800, 480), [(10, 3)])


def test_the_screen_is_only_its_owners(app, paired_client, anonymous_client):
    client, device_id, secret = paired_client
    _upload(client, secret, bytes(FRAME_BYTES))
    stranger = app.test_client()
    stranger.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"

    for path in (f"/device/{device_id}/screen", f"/device/{device_id}/screen/frame.png"):
        assert stranger.get(path).status_code == 404
        assert anonymous_client.get(path).status_code == 401
