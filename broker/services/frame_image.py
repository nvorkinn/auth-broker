"""A screen's frame as a browser can show it: a PNG, the right way up.

The frame is in the screen's format (device_config.format). mono-1bpp-packed is the e-paper's own buffer (see
routes/frames.py): 800x480 landscape, rows top to bottom, 8 pixels a byte with the leftmost in the high bit, and a set
bit is black. png is already a PNG of the panel's landscape picture. A screen hung upright gets a portrait picture that
the renderer turned a quarter anticlockwise to fit its landscape panel, so it is turned back here.
"""

import io
import struct
import zlib
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from broker.models import FrameFormat

WIDTH, HEIGHT = 800, 480
BLACK, WHITE = b"\x00", b"\xff"

# Each byte of the buffer as its 8 pixels, one 8-bit grey byte each.
_PIXELS = [b"".join(BLACK if byte & (0x80 >> bit) else WHITE for bit in range(8)) for byte in range(256)]


def preview(frame: bytes, frame_format: FrameFormat, *, upright: bool) -> bytes:
    """Return the frame, in `frame_format`, as a PNG, turned back to portrait when the screen is hung `upright`."""
    if frame_format == "png":
        return _png_upright(frame) if upright else frame
    return to_png(frame, upright=upright)


def _png_upright(frame: bytes) -> bytes:
    """Turn a PNG frame back to portrait: a quarter clockwise, undoing the renderer's quarter anticlockwise."""
    buf = io.BytesIO()
    Image.open(io.BytesIO(frame)).transpose(Image.Transpose.ROTATE_270).save(buf, format="PNG")
    return buf.getvalue()


def to_png(frame: bytes, *, upright: bool) -> bytes:
    """Return a mono-1bpp-packed frame as an 8-bit greyscale PNG, turned back to portrait when hung `upright`."""
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
