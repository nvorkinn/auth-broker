"""A screen's frame as a browser can show it: the panel's raw 1-bit buffer turned into a PNG.

The buffer is the e-paper's own (see routes/frames.py): 800x480 landscape, rows top to bottom, 8 pixels a byte with
the leftmost in the high bit, and a set bit is black. A screen hung upright gets a portrait picture that the renderer
turned a quarter anticlockwise to fit that buffer, so it is turned back here.
"""

import struct
import zlib

WIDTH, HEIGHT = 800, 480
BLACK, WHITE = b"\x00", b"\xff"

# Each byte of the buffer as its 8 pixels, one 8-bit grey byte each.
_PIXELS = [b"".join(BLACK if byte & (0x80 >> bit) else WHITE for bit in range(8)) for byte in range(256)]


def to_png(frame: bytes, *, upright: bool) -> bytes:
    """Return the frame as an 8-bit greyscale PNG, turned back to portrait when the screen is hung `upright`."""
    pixels = b"".join(_PIXELS[byte] for byte in frame)
    if upright:
        # The renderer put portrait pixel (x, y) at landscape (y, 479 - x), so portrait row y is landscape
        # column y read bottom to top.
        rows = [pixels[y::WIDTH][::-1] for y in range(WIDTH)]
        width, height = HEIGHT, WIDTH
    else:
        rows = [pixels[y * WIDTH : (y + 1) * WIDTH] for y in range(HEIGHT)]
        width, height = WIDTH, HEIGHT
    # Each row starts with its filter type: 0, none.
    data = zlib.compress(b"".join(b"\x00" + row for row in rows), 9)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
            _chunk(b"IDAT", data),
            _chunk(b"IEND", b""),
        ]
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
