"""Pictures for sound buttons, found automatically.

When a sound is imported (from a link, an upload, or reset to its default):

  1. the import page's own thumbnail: og:image, twitter:image, JSON-LD image,
     <link rel=image_src>, or an <img> next to the sound's play button. A
     candidate must be a real picture (decodes, >= 96 px, aspect 0.5-2, not
     blank) and not the site's generic image: no logo/default/share-style
     name, not the site's homepage og:image (same URL or a dHash within 6
     bits), and not a picture the same site uses for 2+ different sounds;
  2. else the meme catalog (meme_catalog.json in the config folder): the sound's
     label, page title and link slug matched against names and aliases;
  3. else, when something was close, a few pictures to choose from.

A picture is "automatic" only if this module put it there (image_auto); any
other picture is the user's and is never replaced. When the user picks or
uploads a picture themselves, the catalog learns it under the sound's label.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from .. import paths
from ..util import load_json_file, write_json_atomic

log = logging.getLogger("sounds.artwork")

CATALOG_FILE = "meme_catalog.json"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 4096 * 4096
MIN_PX = 96
ASPECT = (0.5, 2.0)
MIN_STDDEV = 10.0             # brightness spread below this = a blank / near-blank picture
GENERIC_BITS = 6              # dHash distance at or below which two pictures are "the same"
REPEAT_PAGES = 2              # a picture used for this many different sounds on one site is the site's
HOME_TTL_S = 7 * 24 * 3600
HOME_RETRY_S = 24 * 3600      # after a failed homepage look-up
CONFIDENT = 0.85
UNCERTAIN = 0.5
MAX_CANDIDATES = 6
MAX_CHOICES = 4
BUDGET_S = 25.0               # all the downloads of one look-up
SAVE_PX = 512                 # a picture kept for a button is stored at most this big

STOPWORDS = {
    "sound", "sounds", "effect", "effects", "sfx", "meme", "memes", "mp3", "wav", "ogg", "m4a", "original",
    "the", "a", "an", "of", "button", "buttons", "instant", "instants", "myinstants", "hd", "audio", "clip",
    "soundboard", "ringtone", "free", "download", "official", "full", "version", "en", "www", "com",
}
GENERIC_HINT = re.compile(r"logo|favicon|og[-_]?default|default|share|social|placeholder|banner|brand|sprite|"
                          r"apple-touch-icon", re.IGNORECASE)
ORIGINS = ("page", "catalog", "manual")
CID = re.compile(r"(?:icon|cache):[0-9a-f]{40}")


class ArtError(Exception):
    pass


# ---- text matching (pure) ------------------------------------------------------------------------

def tokens(text: str) -> list[str]:
    """'Vine-Boom Sound Effect 90210 (HD)' -> ['vine', 'boom']."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    out = []
    for w in re.split(r"[^a-z0-9]+", t):
        if not w or w in STOPWORDS or w.isdigit() or (len(w) >= 4 and sum(c.isdigit() for c in w) >= 3):
            continue
        out.append(w)
    return out


def slug_text(url: str) -> str:
    """The words of a link's last meaningful path segment ('/en/instant/vine-boom-90210/' -> 'vine boom 90210')."""
    try:
        parts = [p for p in urllib.parse.urlsplit(url or "").path.split("/") if p]
    except ValueError:
        return ""
    for seg in reversed(parts):
        seg = re.sub(r"\.[a-z0-9]{2,4}$", "", urllib.parse.unquote(seg), flags=re.IGNORECASE)
        if tokens(seg):
            return re.sub(r"[-_+]+", " ", seg)
    return ""


def similarity(a: list[str], b: list[str]) -> float:
    """Token-set similarity (Dice): 1.0 = same words."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return 2 * len(sa & sb) / (len(sa) + len(sb))


# ---- page candidates (pure) ----------------------------------------------------------------------

_VOID = {"img", "meta", "link", "br", "input", "source", "hr", "area", "base", "col", "embed", "param", "track", "wbr"}
_PLAYISH = re.compile(r"\.(?:mp3|wav|ogg|m4a)\b|\bplay\s*\(", re.IGNORECASE)


class _Pictures(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: list[str] = []      # og:image / twitter:image, in that order
        self.image_src: list[str] = []
        self.near_audio: list[str] = []
        self.jsonld: list[str] = []
        self._frames: list[dict] = []
        self._in_jsonld = False
        self._jsonld_buf = ""
        self._og: list[str] = []
        self._tw: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url") and a.get("content"):
                self._og.append(a["content"])
            elif prop in ("twitter:image", "twitter:image:src") and a.get("content"):
                self._tw.append(a["content"])
            return
        if tag == "link":
            if "image_src" in a.get("rel", "").lower().split() and a.get("href"):
                self.image_src.append(a["href"])
            return
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld, self._jsonld_buf = True, ""
        playish = tag in ("audio", "source") or any(_PLAYISH.search(a.get(k, "")) for k in ("onclick", "data-url", "href",
                                                                                             "onmousedown", "data-src"))
        if playish and self._frames:
            self._frames[-1]["audio"] = True
        if tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            if src:
                for f in self._frames[-4:]:
                    f["imgs"].append(src)
            return
        if tag not in _VOID:
            self._frames.append({"tag": tag, "imgs": [], "audio": playish and tag not in ("audio", "source")})

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.extend(_jsonld_images(self._jsonld_buf))
        for i in range(len(self._frames) - 1, -1, -1):
            if self._frames[i]["tag"] == tag:
                frame = self._frames.pop(i)
                del self._frames[i:]
                if frame["audio"]:
                    if frame["imgs"] and not self.near_audio:
                        self.near_audio.extend(frame["imgs"][:3])
                    elif self._frames:
                        self._frames[-1]["audio"] = True  # look one level further out
                break

    def handle_data(self, data):
        if self._in_jsonld and len(self._jsonld_buf) < 200_000:
            self._jsonld_buf += data

    def close(self):
        super().close()
        self.meta = self._og + self._tw


def _jsonld_images(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[str] = []

    def walk(x, depth=0):
        if depth > 6 or len(out) >= 4:
            return
        if isinstance(x, list):
            for y in x:
                walk(y, depth + 1)
        elif isinstance(x, dict):
            img = x.get("image") or x.get("thumbnailUrl")
            if isinstance(img, str):
                out.append(img)
            elif isinstance(img, dict) and isinstance(img.get("url"), str):
                out.append(img["url"])
            elif isinstance(img, list):
                out.extend(i if isinstance(i, str) else (i or {}).get("url", "") for i in img[:3] if i)
            for k in ("@graph", "mainEntity", "associatedMedia"):
                if k in x:
                    walk(x[k], depth + 1)

    walk(data)
    return [u for u in out if isinstance(u, str) and u]


def page_candidates(html: str, base: str) -> list[str]:
    """Pure: picture addresses a sound page offers, best first, absolute http(s), no duplicates."""
    p = _Pictures()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001 - broken HTML: use what was parsed
        p.meta = p._og + p._tw
    out: list[str] = []
    for raw in p.meta + p.jsonld + p.image_src + p.near_audio:
        url = urllib.parse.urljoin(base, raw.strip())
        if urllib.parse.urlsplit(url).scheme in ("http", "https") and url not in out:
            out.append(url)
    return out[:MAX_CANDIDATES]


def homepage_image(html: str, base: str) -> str:
    p = _Pictures()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001
        p.meta = p._og + p._tw
    return urllib.parse.urljoin(base, p.meta[0]) if p.meta else ""


def url_looks_generic(url: str) -> bool:
    """Pure: a logo / default / share picture by its name."""
    path = urllib.parse.urlsplit(url or "").path
    return bool(GENERIC_HINT.search("/".join(path.split("/")[-2:])))


# ---- pictures (pure) --------------------------------------------------------------------------------

def decode(data: bytes):
    """Bytes -> RGB Pillow image, if it's a real picture big and busy enough; else ArtError."""
    from PIL import Image, ImageStat
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ArtError("too big or empty")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG", "WEBP", "GIF", "BMP"):
            raise ArtError(f"not a picture ({img.format})")
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise ArtError("too many pixels")
        img.load()
    except ArtError:
        raise
    except Exception as e:  # noqa: BLE001 - Pillow raises many kinds
        raise ArtError(f"not a picture ({e})") from None
    if min(w, h) < MIN_PX:
        raise ArtError("too small")
    if not ASPECT[0] <= w / h <= ASPECT[1]:
        raise ArtError("odd shape")
    rgb = img.convert("RGB")
    small = rgb.copy()
    small.thumbnail((64, 64))
    if ImageStat.Stat(small.convert("L")).stddev[0] < MIN_STDDEV:
        raise ArtError("blank")
    return rgb


def dhash(img) -> int:
    """64-bit difference hash."""
    from PIL import Image
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata()) if not hasattr(g, "get_flattened_data") else list(g.get_flattened_data())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & (2 ** 64 - 1)).count("1")


def png_of(img, px: int = SAVE_PX) -> bytes:
    img = img.copy()
    img.thumbnail((px, px))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---- the catalog ------------------------------------------------------------------------------------

class Catalog:
    """meme_catalog.json: {"version": 1, "entries": [{name, aliases[], image_id?, picture_url?, sources[]}]}"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.config_dir() / CATALOG_FILE
        self._lock = threading.Lock()
        raw = load_json_file(self.path, {}) or {}
        entries = raw.get("entries") if isinstance(raw, dict) else None
        self.entries: list[dict] = [self._clean(e) for e in entries or [] if isinstance(e, dict)]

    @staticmethod
    def _clean(e: dict) -> dict:
        def strs(x, n):
            return [str(v)[:300] for v in (x or [])[:n] if isinstance(v, str) and v.strip()]
        image = str(e.get("image_id") or "")
        return {"name": str(e.get("name") or "")[:120], "aliases": strs(e.get("aliases"), 24),
                "image_id": image if re.fullmatch(r"[0-9a-f]{40}", image) else "",
                "picture_url": str(e.get("picture_url") or "")[:1024], "sources": strs(e.get("sources"), 8)}

    def save(self) -> None:
        with self._lock:
            write_json_atomic(self.path, {"version": 1, "entries": self.entries}, private=False, ensure_ascii=False)

    def match(self, texts: list[str]) -> list[tuple[float, dict]]:
        """Best entries for these texts (label, title, slug), best first, score 0..1."""
        queries = [tokens(t) for t in texts if t]
        queries = [q for q in queries if q]
        scored = []
        for e in self.entries:
            best = 0.0
            for alias in [e["name"], *e["aliases"]]:
                at = tokens(alias)
                if not at:
                    continue
                for q in queries:
                    s = 1.0 if " ".join(at) == " ".join(q) else similarity(at, q)
                    best = max(best, s)
            if best >= UNCERTAIN:
                scored.append((best, e))
        scored.sort(key=lambda x: -x[0])
        return scored

    def upsert(self, name: str, aliases=(), image_id: str = "", picture_url: str = "", sources=(),
               replace_image: bool = False) -> tuple[dict, bool]:
        """Add or merge an entry (same normalized name or alias); returns (entry, created)."""
        key = " ".join(tokens(name))
        with self._lock:
            entry = None
            for e in self.entries:
                if key and key in {" ".join(tokens(a)) for a in [e["name"], *e["aliases"]]}:
                    entry = e
                    break
            created = entry is None
            if created:
                entry = {"name": name[:120], "aliases": [], "image_id": "", "picture_url": "", "sources": []}
                self.entries.append(entry)
            for a in [name, *aliases]:
                if a and a not in entry["aliases"] and a != entry["name"] and len(entry["aliases"]) < 24:
                    entry["aliases"].append(a[:300])
            for s in sources:
                if s and s not in entry["sources"] and len(entry["sources"]) < 8:
                    entry["sources"].append(s[:300])
            if image_id and (replace_image or not entry["image_id"]):
                entry["image_id"] = image_id
            if picture_url and not entry["picture_url"]:
                entry["picture_url"] = picture_url[:1024]
        return entry, created

    def learn(self, label: str, image_id: str) -> None:
        """The user picked this picture for a sound called `label`: remember it."""
        if not tokens(label) or not re.fullmatch(r"[0-9a-f]{40}", image_id or ""):
            return
        self.upsert(label, image_id=image_id, replace_image=True)
        self.save()
        log.info("meme catalog: learned a picture for %r", label)


def import_sources(catalog: Catalog, path: Path, has_image: Callable[[str], bool],
                   import_icon: Callable[[bytes], str]) -> dict:
    """sources.json (a list of {slot, label, picture, image, source, icon_file, ...}) -> catalog entries.
    Idempotent. Pictures: the `image` id if PandaDeck already has it, else `icon_file`
    (next to the json) imported, else `picture` kept for a download when first matched."""
    path = Path(path)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ArtError(f"can't read {path.name}: {e}") from None
    if isinstance(items, dict):
        items = items.get("sounds") or items.get("entries") or []
    if not isinstance(items, list):
        raise ArtError("expected a list of sounds")
    added = updated = 0
    for it in items[:500]:
        if not isinstance(it, dict) or not str(it.get("label") or "").strip():
            continue
        label = str(it["label"]).strip()
        sources = [str(it[k]) for k in ("source", "audio_source") if isinstance(it.get(k), str) and it[k]]
        aliases = [s for s in (slug_text(src) for src in sources[:1]) if s]
        image = str(it.get("image") or "")
        if not (re.fullmatch(r"[0-9a-f]{40}", image) and has_image(image)):
            image = ""
            icon_file = str(it.get("icon_file") or "")
            if icon_file:
                f = (path.parent / icon_file).resolve()
                if path.parent.resolve() in f.parents and f.is_file():
                    try:
                        image = import_icon(f.read_bytes())
                    except Exception as e:  # noqa: BLE001 - a bad picture file: keep going
                        log.info("seed %s: icon_file unusable: %s", label, e)
        picture = str(it.get("picture") or "")
        picture = picture if picture.startswith("https://") else ""
        _, created = catalog.upsert(label, aliases, image_id=image, picture_url=picture, sources=sources)
        added += created
        updated += not created
    catalog.save()
    return {"added": added, "updated": updated, "entries": len(catalog.entries)}


# ---- per-site memory (homepage pictures, pictures used for many sounds) --------------------------------

class SiteMemory:
    def __init__(self, folder: Path, clock: Callable[[], float] = time.time):
        self.folder, self._clock = Path(folder), clock
        self._lock = threading.Lock()
        self.homes: dict = load_json_file(self.folder / "homes.json", {}) or {}
        self.repeats: dict = load_json_file(self.folder / "repeats.json", {}) or {}

    def home(self, host: str) -> dict | None:
        h = self.homes.get(host)
        if not isinstance(h, dict):
            return None
        ttl = HOME_TTL_S if h.get("ok") else HOME_RETRY_S
        return h if self._clock() - float(h.get("t", 0)) < ttl else None

    def set_home(self, host: str, url: str, hash_: int | None) -> None:
        with self._lock:
            self.homes[host] = {"t": self._clock(), "ok": True, "url": url, "dhash": f"{hash_:016x}" if hash_ is not None else ""}
            self._save("homes.json", self.homes)

    def repeated(self, host: str, page: str, hash_: int) -> bool:
        """Record `hash_` for `page`; True when another page of the host has (nearly) the same picture."""
        with self._lock:
            table = self.repeats.setdefault(host, [])
            pages: set = set()
            hit = None
            for row in table:
                if hamming(int(row["d"], 16), hash_) <= GENERIC_BITS:
                    pages.update(row["p"])
                    hit = hit or row
            if hit is None:
                hit = {"d": f"{hash_:016x}", "p": []}
                table.append(hit)
                del table[:-200]
            if page not in hit["p"]:
                hit["p"] = (hit["p"] + [page])[-6:]
            pages.add(page)
            self._save("repeats.json", self.repeats)
            return len(pages) >= REPEAT_PAGES

    def _save(self, name: str, data) -> None:
        try:
            write_json_atomic(self.folder / name, data, private=False)
        except OSError as e:
            log.debug("artwork memory not saved: %s", e)


# ---- the resolver ------------------------------------------------------------------------------------

class Artwork:
    def __init__(self, catalog: Catalog, cache_dir: Path, fetcher_factory: Callable[[], object],
                 save_picture: Callable[[bytes], str], has_picture: Callable[[str], bool],
                 clock: Callable[[], float] = time.time):
        """fetcher_factory() -> object with get(url, cap) (sounds.fetch.Fetcher: https, public addresses only);
        save_picture(png) -> picture id (deck/userimg.save_upload); has_picture(id) -> bool."""
        self.catalog = catalog
        self.folder = Path(cache_dir) / "artwork"
        self._fetcher = fetcher_factory
        self._save, self._has = save_picture, has_picture
        self._clock = clock
        self.sites = SiteMemory(self.folder, clock)
        self._lock = threading.Lock()
        self.pending: dict = load_json_file(self.folder / "pending.json", {}) or {}

    # -- public --------------------------------------------------------------------------------
    def resolve(self, label: str, link: str = "", page: tuple[str, str] | None = None, title: str = "") -> dict:
        """-> {"status": "auto"|"choose"|"none", "assign": {...}|None, "choices": [...]}"""
        deadline = self._clock() + BUDGET_S
        fetcher = self._fetcher()
        page_url, html = page if page else ("", "")
        if link and not html:
            try:
                page_url, ctype, body = fetcher.get(link, 2 * 1024 * 1024)
                html = body.decode("utf-8", "replace") if "html" in ctype or body.lstrip()[:1] == b"<" else ""
            except Exception as e:  # noqa: BLE001 - FetchError, network: no page pictures then
                log.info("artwork: page %s unavailable: %s", link, e)
        page_url = page_url or link
        if html and not title:
            from .fetch import page_title
            title = page_title(html)
        # 1. the page's own picture
        for url in page_candidates(html, page_url) if html else []:
            if self._clock() > deadline:
                break
            pic = self._page_picture(fetcher, url, page_url, deadline)
            if pic is not None:
                return {"status": "auto", "assign": {"image": self._save(png_of(pic)), "origin": "page", "src": url},
                        "choices": []}
        # 2. the catalog
        matches = self.catalog.match([label, title, slug_text(page_url)])
        if matches and matches[0][0] >= CONFIDENT:
            image = self._entry_picture(fetcher, matches[0][1], deadline)
            if image:
                return {"status": "auto", "assign": {"image": image, "origin": "catalog",
                                                     "src": matches[0][1]["name"]}, "choices": []}
        # 3. something close: let the user choose
        choices = []
        for score, entry in matches[:MAX_CHOICES]:
            if self._clock() > deadline:
                break
            c = self._entry_choice(fetcher, entry, deadline)
            if c:
                choices.append({**c, "name": entry["name"], "score": round(score, 2)})
        return {"status": "choose" if choices else "none", "assign": None, "choices": choices}

    def remember_choices(self, key: str, choices: list) -> None:
        with self._lock:
            if choices:
                self.pending[key] = choices
            else:
                self.pending.pop(key, None)
            while len(self.pending) > 64:
                self.pending.pop(next(iter(self.pending)))
            self._save_pending()

    def choices(self, key: str) -> list:
        with self._lock:
            return list(self.pending.get(key) or [])

    def choose(self, cid: str, key: str = "") -> str:
        """A picture the user picked from the choices -> its picture id."""
        if not CID.fullmatch(cid or ""):
            raise ArtError("unknown picture")
        kind, ref = cid.split(":", 1)
        if kind == "icon":
            if not self._has(ref):
                raise ArtError("that picture is gone")
            image = ref
        else:
            f = self.folder / "choices" / f"{ref}.png"
            if not f.is_file():
                raise ArtError("that picture is gone")
            image = self._save(f.read_bytes())
        if key:
            self.remember_choices(key, [])
        return image

    def thumb(self, cid: str) -> bytes | None:
        if not CID.fullmatch(cid or "") or not cid.startswith("cache:"):
            return None
        f = self.folder / "choices" / f"{cid[6:]}.png"
        return f.read_bytes() if f.is_file() else None

    # -- internals -----------------------------------------------------------------------------
    def _download(self, fetcher, url: str, deadline: float):
        if self._clock() > deadline:
            raise ArtError("out of time")
        _, _, data = fetcher.get(url, MAX_IMAGE_BYTES)
        return decode(data)

    def _page_picture(self, fetcher, url: str, page_url: str, deadline: float):
        if url_looks_generic(url):
            log.debug("artwork: %s looks like a site picture", url)
            return None
        try:
            img = self._download(fetcher, url, deadline)
        except Exception as e:  # noqa: BLE001
            log.debug("artwork: %s rejected: %s", url, e)
            return None
        h = dhash(img)
        host = (urllib.parse.urlsplit(page_url).hostname or "").lower()
        home = self._home(fetcher, host, deadline) if host else None
        if home and (home.get("url") == url or (home.get("dhash") and hamming(int(home["dhash"], 16), h) <= GENERIC_BITS)):
            log.debug("artwork: %s is %s's default picture", url, host)
            return None
        if host and self.sites.repeated(host, page_url, h):
            log.debug("artwork: %s is used for several sounds on %s", url, host)
            return None
        return img

    def _home(self, fetcher, host: str, deadline: float) -> dict | None:
        known = self.sites.home(host)
        if known is not None:
            return known
        url, h = "", None
        try:
            base, ctype, body = fetcher.get(f"https://{host}/", 2 * 1024 * 1024)
            url = homepage_image(body.decode("utf-8", "replace"), base)
            if url:
                h = dhash(self._download(fetcher, url, deadline))
        except Exception as e:  # noqa: BLE001 - no homepage picture: nothing to compare with
            log.debug("artwork: homepage of %s: %s", host, e)
        self.sites.set_home(host, url, h)
        return self.sites.home(host)

    def _entry_picture(self, fetcher, entry: dict, deadline: float) -> str:
        if entry.get("image_id") and self._has(entry["image_id"]):
            return entry["image_id"]
        if entry.get("picture_url"):
            try:
                image = self._save(png_of(self._download(fetcher, entry["picture_url"], deadline)))
            except Exception as e:  # noqa: BLE001
                log.info("artwork: catalog picture for %r unavailable: %s", entry["name"], e)
                return ""
            entry["image_id"] = image   # downloaded once, kept in PandaDeck's icons from now on
            self.catalog.save()
            return image
        return ""

    def _entry_choice(self, fetcher, entry: dict, deadline: float) -> dict | None:
        if entry.get("image_id") and self._has(entry["image_id"]):
            return {"cid": "icon:" + entry["image_id"], "thumb": "/api/icons/get?id=" + entry["image_id"]}
        if entry.get("picture_url"):
            try:
                png = png_of(self._download(fetcher, entry["picture_url"], deadline), 256)
            except Exception:  # noqa: BLE001
                return None
            ref = hashlib.sha1(png).hexdigest()  # noqa: S324 - a content address
            f = self.folder / "choices" / f"{ref}.png"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(png)
            return {"cid": "cache:" + ref, "thumb": "/api/sounds/artwork/thumb?cid=cache:" + ref}
        return None

    def _save_pending(self) -> None:
        try:
            write_json_atomic(self.folder / "pending.json", self.pending, private=False)
        except OSError as e:
            log.debug("pending choices not saved: %s", e)


# ---- what a sound's picture becomes (pure) --------------------------------------------------------------

def is_manual(entry: dict) -> bool:
    """The user's own choice (a picture they set, or "keep the symbol"): never replaced automatically."""
    return entry.get("image_origin") == "manual" or bool(entry.get("image")) and not entry.get("image_auto")


def apply_decision(entry: dict, decision: dict) -> dict:
    """A sound's settings after a look-up (its sound was imported or replaced)."""
    out = dict(entry)
    if is_manual(entry):
        return out
    assign = (decision or {}).get("assign")
    if assign:
        out.update(image=assign["image"], image_auto=True, image_origin=assign["origin"], image_src=assign.get("src", "")[:512])
    else:  # nothing (sure) found: never keep an automatic picture that belonged to the old sound
        out.update(image="", image_auto=False, image_origin="", image_src="")
    return out
