"""Pictures the user uploads for deck buttons.

Uploads (PNG / JPEG / WebP / ICO / BMP, at most 2 MB) are decoded with
Pillow under a pixel budget (no decompression bombs), flattened to RGBA,
trimmed to their visible content, scaled to at most 256 px and stored as
`<config>/icons/<sha1>.png`. The sha1 of that normalized PNG is the
picture's id in settings. The panel gets them through the same pipeline as
app icons (56 px RGB565A8).
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from PIL import Image

from ..music import images as im
from ..paths import config_dir

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4096 * 4096          # decoded size budget, checked before decoding
STORE_PX = 256
FORMATS = {"PNG", "JPEG", "WEBP", "ICO", "BMP"}
ID_RE = re.compile(r"^[0-9a-f]{40}$")


class ImageError(ValueError):
    """Shown to the user as is."""


def icons_dir() -> Path:
    return config_dir() / "icons"


def path_for(image_id: str) -> Path | None:
    if not ID_RE.match(image_id or ""):
        return None
    return icons_dir() / f"{image_id}.png"


def normalize(data: bytes) -> Image.Image:
    """Bytes from the browser -> a trimmed RGBA image (raises ImageError)."""
    if not data:
        raise ImageError("That file is empty")
    if len(data) > MAX_BYTES:
        raise ImageError("Pictures can be at most 2 MB")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in FORMATS:
            raise ImageError("Use a PNG, JPG, WebP, ICO or BMP picture")
        w, h = img.size
        if w < 1 or h < 1 or w * h > MAX_PIXELS:
            raise ImageError("That picture is too large (max 4096 x 4096 pixels)")
        if img.format == "ICO":  # pick the biggest size inside the .ico
            sizes = sorted(getattr(img, "ico", None).sizes() if hasattr(img, "ico") else [img.size])
            if sizes:
                img.size = sizes[-1]  # noqa: B010 - Pillow's IcoImagePlugin API
        img.load()
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("That picture is too large") from None
    except Exception:  # noqa: BLE001 - truncated / not an image / unsupported
        raise ImageError("That file isn't a picture PandaDeck can read") from None
    img = img.convert("RGBA")
    img = im.trim(img)
    if max(img.size) > STORE_PX:
        img.thumbnail((STORE_PX, STORE_PX), Image.LANCZOS)
    return img


def save_upload(data: bytes) -> str:
    """Validate, normalize and store; returns the picture id (sha1)."""
    img = normalize(data)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    png = buf.getvalue()
    image_id = hashlib.sha1(png).hexdigest()
    path = icons_dir() / f"{image_id}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(path)
    return image_id


def load(image_id: str) -> Image.Image | None:
    path = path_for(image_id)
    if path is None or not path.exists():
        return None
    try:
        with Image.open(path) as img:
            if img.size[0] * img.size[1] > STORE_PX * STORE_PX * 4:
                return None
            # square photos/logos get rounded corners like app icons (baked into
            # the pixels, so the editor preview and the panel look the same)
            return im.rounded_if_square(img.convert("RGBA"))
    except Exception:  # noqa: BLE001 - damaged file: behave as if missing
        return None


def png_bytes(image_id: str) -> bytes | None:
    """The picture as the panel will show it (for the editor preview)."""
    img = load(image_id)
    if img is None:
        return None
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def panel_bytes(image_id: str, px: int = im.ICON_PX) -> bytes | None:
    """RGB565A8 at the panel's icon size (ICON_PX_LARGE for the "icons" deck style)."""
    img = load(image_id)
    return im.to_rgb565a8(img, px) if img is not None else None
