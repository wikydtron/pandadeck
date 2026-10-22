"""Image conversion for the panel (pure).

    cover    248 px square RGBA with rounded corners -> RGB565A8 icon "art"
    backdrop 200x108 blurred, darkened RGB          -> RGB565 (panel scales x4)
    app icon trimmed, squared, 56 px                 -> RGB565A8
"""
from __future__ import annotations

import colorsys

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

ART_PX = 248
ART_RADIUS = 16
BACKDROP = (200, 108)         # the panel scales it x4 to the 800x432 page
BACKDROP_PHOTO = (400, 216)   # artist photos: the firmware's max source size, scaled x2 (sharp)
ART_BG = (20, 20, 19, 255)    # COL_BG: transparent covers sit on the panel's background
ICON_PX = 56
ICON_PX_LARGE = 96   # deck style "icons" without names: the picture fills the 113 px tile
ICON_PX_NAMED = 80   # deck style "icons" with the name under the picture
DEFAULT_TINT = "#D97757"


def tint_from(img: Image.Image) -> str:
    """A readable accent colour from the cover (vivid, not too dark)."""
    small = img.convert("RGB").resize((24, 24), Image.BILINEAR)
    best, best_score = (217, 119, 87), -1.0
    raw = small.tobytes()
    for i in range(0, len(raw), 3):
        r, g, b = raw[i] / 255, raw[i + 1] / 255, raw[i + 2] / 255
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        score = s * (1 - abs(l - 0.55))  # saturated mid-tones win
        if score > best_score:
            best_score, best = score, (h, l, s)
    if best_score < 0.08:  # greyscale cover: use a neutral warm ivory
        return "#D9D4C7"
    h, l, s = best
    r, g, b = colorsys.hls_to_rgb(h, min(max(l, 0.55), 0.70), max(s, 0.55))
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def art_image(img: Image.Image, px: int = ART_PX, radius: int = ART_RADIUS) -> Image.Image:
    """Cover -> square px*px RGBA with anti-aliased rounded corners baked into
    the alpha plane (LVGL corner clipping would cost a layer render per frame)."""
    img = img.convert("RGBA")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    flat = Image.new("RGBA", img.size, ART_BG)
    flat.alpha_composite(img)
    out = flat.resize((px, px), Image.LANCZOS)
    ss = 4  # draw the mask big, then downsample = smooth corners
    mask = Image.new("L", (px * ss, px * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, px * ss - 1, px * ss - 1), radius=radius * ss, fill=255)
    out.putalpha(mask.resize((px, px), Image.LANCZOS))
    return out


BACKDROP_MEAN = 0.20  # target average brightness (0..1) so white text always reads


def _dim_to(img: Image.Image, cap: float, target_mean: float = BACKDROP_MEAN) -> Image.Image:
    """Darken by `cap` or more: light pictures (white covers, bright photos) get
    darkened until their average brightness is at most `target_mean`."""
    mean = sum(img.convert("L").resize((32, 32)).tobytes()) / (32 * 32 * 255)
    return ImageEnhance.Brightness(img).enhance(min(cap, target_mean / max(mean, 0.01)))


def backdrop_image(img: Image.Image, size: tuple[int, int] = BACKDROP, focus_top: bool = False) -> Image.Image:
    """Cover (or artist photo) -> small blurred, darkened RGB backdrop,
    cropped to the page's aspect (centred; upper third for photos, where faces
    are), darker towards the bottom where the controls sit."""
    img = img.convert("RGB")
    w, h = size
    src_w, src_h = img.size
    if src_w * h > src_h * w:   # wider than the page: crop the sides
        crop_w = max(1, int(src_h * w / h))
        left = (src_w - crop_w) // 2
        img = img.crop((left, 0, left + crop_w, src_h))
    else:                        # taller: crop top/bottom
        crop_h = max(1, min(src_h, int(src_w * h / w)))
        top = (src_h - crop_h) // (3 if focus_top else 2)
        img = img.crop((0, top, src_w, top + crop_h))
    img = img.resize(size, Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(radius=6))
    img = ImageEnhance.Color(img).enhance(1.2)
    img = _dim_to(img, 0.40)
    shade = Image.linear_gradient("L").resize(size)  # 0 at the top -> 255 at the bottom
    black = Image.new("RGB", size, (0, 0, 0))
    return Image.composite(black, img, shade.point(lambda v: v * 45 // 100))


def photo_backdrop_image(img: Image.Image, size: tuple[int, int] = BACKDROP_PHOTO) -> Image.Image:
    """Artist photo -> a SHARP darkened backdrop (unlike the blurred cover): cropped
    to the page's aspect around the upper third (faces), dimmed so white text stays
    readable, and darker towards the bottom where the controls sit."""
    img = img.convert("RGB")
    w, h = size
    src_w, src_h = img.size
    if src_w * h > src_h * w:
        crop_w = max(1, int(src_h * w / h))
        left = (src_w - crop_w) // 2
        img = img.crop((left, 0, left + crop_w, src_h))
    else:
        crop_h = max(1, min(src_h, int(src_w * h / w)))
        top = (src_h - crop_h) // 3
        img = img.crop((0, top, src_w, top + crop_h))
    img = img.resize(size, Image.LANCZOS)
    img = _dim_to(img, 0.55)
    shade = Image.linear_gradient("L").resize(size)  # 0 at the top -> 255 at the bottom
    black = Image.new("RGB", size, (0, 0, 0))
    return Image.composite(black, img, shade.point(lambda v: v * 55 // 100))


def encode_rgb565(img: Image.Image) -> bytes:
    """RGB -> raw RGB565 little-endian (LVGL's native 16-bit order)."""
    raw = img.convert("RGB").tobytes()
    n = len(raw) // 3
    out = bytearray(n * 2)
    for i in range(n):
        r, g, b = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out[2 * i] = v & 0xFF
        out[2 * i + 1] = v >> 8
    return bytes(out)


def encode_rgb565a8(img: Image.Image) -> bytes:
    """RGBA -> LVGL RGB565A8 (RGB565 LE plane, then an A8 plane), as-is."""
    img = img.convert("RGBA")
    n = img.width * img.height
    rgb = bytearray(n * 2)
    alpha = bytearray(n)
    raw = img.tobytes()
    for i in range(n):
        r, g, b, a = raw[4 * i], raw[4 * i + 1], raw[4 * i + 2], raw[4 * i + 3]
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        rgb[2 * i] = v & 0xFF
        rgb[2 * i + 1] = v >> 8
        alpha[i] = a
    return bytes(rgb) + bytes(alpha)


def _content_box(img: Image.Image, edge: int, threshold: int):
    """Bounding box of pixels with alpha >= threshold, ignoring an `edge` band."""
    w, h = img.size
    mask = img.getchannel("A").crop((edge, edge, w - edge, h - edge)).point(lambda v: 255 if v >= threshold else 0)
    bb = mask.getbbox()
    return (bb[0] + edge, bb[1] + edge, bb[2] + edge, bb[3] + edge) if bb else None


def trim(img: Image.Image, edge: int = 3, threshold: int = 48) -> Image.Image:
    """Crop to the visible content, squared and centred.

    Apps that only ship a small icon come back as a tiny glyph inside a big
    transparent frame (sometimes with a 1 px thumbnail border), so they looked
    half-size next to the others. The outer `edge` pixels are ignored when
    finding the content so that border doesn't count."""
    img = img.convert("RGBA")
    w, h = img.size
    if min(w, h) <= 2 * edge + 2:
        edge = 0  # too small to have a frame worth ignoring
    box = _content_box(img, edge, threshold)
    if not box:
        return img
    # Windows sometimes returns a small icon on a faint bordered "thumbnail plate"
    # with a drop shadow a few pixels in from the edge: the pass above then finds the
    # plate, not the icon. Look for the solid pixels inside it instead, and use them
    # only when they're clearly smaller (real full-size icons are left alone).
    if box[2] - box[0] >= 0.9 * w and box[3] - box[1] >= 0.9 * h:
        band = max(edge, round(min(w, h) * 0.06))
        solid = _content_box(img, band, 200) if min(w, h) > 2 * band + 2 else None
        if solid and max(solid[2] - solid[0], solid[3] - solid[1]) < 0.7 * min(w, h):
            pad = round(max(solid[2] - solid[0], solid[3] - solid[1]) * 0.04)
            box = (solid[0] - pad, solid[1] - pad, solid[2] + pad, solid[3] + pad)
    x0, y0, x1, y1 = box
    side = max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    box = (round(cx - side / 2), round(cy - side / 2), round(cx - side / 2) + side, round(cy - side / 2) + side)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(img.crop(box), (0, 0))
    return out


def round_corners(img: Image.Image, frac: float = 0.22) -> Image.Image:
    """Pure: anti-aliased rounded corners baked into the alpha (radius = frac x side)."""
    img = img.convert("RGBA")
    w, h = img.size
    ss = 4
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=round(min(w, h) * frac * ss),
                                           fill=255)
    alpha = ImageChops.multiply(img.getchannel("A"), mask.resize((w, h), Image.LANCZOS))
    out = img.copy()
    out.putalpha(alpha)
    return out


def rounded_if_square(img: Image.Image) -> Image.Image:
    """Pure: a user picture with solid square corners (a photo, a square logo) gets
    app-icon-like rounded corners; pictures that already have a shape are left alone."""
    img = img.convert("RGBA")
    w, h = img.size
    a = img.getchannel("A")
    corners = [a.getpixel(xy) for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    return round_corners(img) if min(corners) >= 250 else img


def icon_image(img: Image.Image, px: int = ICON_PX) -> Image.Image:
    return trim(img).resize((px, px), Image.LANCZOS)


def to_rgb565a8(img: Image.Image, px: int = ICON_PX) -> bytes:
    """App icon (RGBA, any size) -> trimmed px*px RGB565A8 for the panel."""
    return encode_rgb565a8(icon_image(img, px))


def png_bytes(img: Image.Image, px: int | None = None) -> bytes:
    """For the settings page (app pickers, now-playing preview)."""
    import io
    if px:
        img = img.copy()
        img.thumbnail((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
