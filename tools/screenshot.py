"""Grab what the panel is showing right now as a PNG (GET /api/screen).

usage: python tools/screenshot.py [out.png] [--url http://pandadeck.local]
"""
from __future__ import annotations

import argparse
import array
import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="panel.png")
    ap.add_argument("--url")
    args = ap.parse_args()
    secrets_path = Path.home() / ".pandadeck" / "secrets.json"
    secrets = json.loads(secrets_path.read_text()) if secrets_path.exists() else {}
    url = (args.url or secrets.get("panel_url") or "http://pandadeck.local").rstrip("/")
    req = urllib.request.Request(f"{url}/api/screen", headers={"X-Panda-Token": secrets.get("token", "")})
    with urllib.request.urlopen(req, timeout=20) as r:
        w, h = int(r.headers.get("X-Width", 800)), int(r.headers.get("X-Height", 480))
        rotate = r.headers.get("X-Rotate", "0") == "180"
        raw = r.read()
    if len(raw) != w * h * 2:
        raise SystemExit(f"short read: {len(raw)} bytes")
    # RGB565, little-endian 16-bit words (R in the high bits). Decode explicitly
    # rather than trusting a PIL raw-mode name.
    px = array.array("H")
    px.frombytes(raw)
    if sys.byteorder != "little":
        px.byteswap()
    rgb = bytearray(w * h * 3)
    for i, v in enumerate(px):
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        j = i * 3
        rgb[j] = (r << 3) | (r >> 2)
        rgb[j + 1] = (g << 2) | (g >> 4)
        rgb[j + 2] = (b << 3) | (b >> 2)
    img = Image.frombytes("RGB", (w, h), bytes(rgb))
    if rotate:  # framebuffer is in panel scan order; show it the way you see it
        img = img.rotate(180)
    img.save(args.out)
    print(f"saved {args.out} ({w}x{h})")


if __name__ == "__main__":
    main()
