"""Flash a firmware .bin to the panel over WiFi.

Works against both:
  * the stock BTT firmware's ElegantOTA page (multipart "firmware" + "MD5"),
  * PandaDeck's own /update (same multipart + X-Panda-Token header).

usage: python tools/flash_ota.py <firmware.bin> [--url http://pandadeck.local] [--token ...]
Token defaults to %USERPROFILE%/.pandadeck/secrets.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def multipart(fields: dict[str, str], file_field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + payload + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("--url")
    ap.add_argument("--token")
    args = ap.parse_args()

    secrets_path = Path.home() / ".pandadeck" / "secrets.json"
    secrets = json.loads(secrets_path.read_text()) if secrets_path.exists() else {}
    url = (args.url or secrets.get("panel_url") or "http://pandadeck.local").rstrip("/")
    token = args.token or secrets.get("token", "")

    data = Path(args.bin).read_bytes()
    if data[:1] != b"\xe9":
        print("not an ESP32 app image (missing 0xE9 magic)")
        return 2
    md5 = hashlib.md5(data).hexdigest()  # noqa: S324 - ElegantOTA integrity field, not security
    body, ctype = multipart({"MD5": md5}, "firmware", "firmware", data)
    req = urllib.request.Request(f"{url}/update", data=body, method="POST",
                                 headers={"Content-Type": ctype, "X-Panda-Token": token})
    print(f"uploading {len(data):,} bytes to {url}/update (md5 {md5}) ...")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            print(f"HTTP {r.status} in {time.time() - t0:.1f}s: {r.read().decode(errors='replace')[:200]}")
            return 0 if r.status == 200 else 1
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"upload failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
