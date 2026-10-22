"""Automatic pictures for imported sounds: page thumbnails (and rejecting a
site's generic pictures), the meme catalog (matching tiers, seeding, learning),
picture choices, and keeping the user's own pictures. Local fixtures only."""
import hashlib
import io
import json
import threading

import pytest
from PIL import Image, ImageDraw

from pandadeck.sounds import artwork as aw
from pandadeck.sounds.fetch import FetchError


def picture(seed: int, size=(200, 200), fmt="JPEG") -> bytes:
    """A busy, distinct picture per seed."""
    img = Image.new("RGB", size, (seed * 37 % 256, seed * 91 % 256, seed * 53 % 256))
    d = ImageDraw.Draw(img)
    for i in range(12):
        x = (seed * 29 + i * 41) % size[0]
        y = (seed * 17 + i * 67) % size[1]
        d.rectangle([x, y, x + 30 + i * 3, y + 20 + i * 2], fill=((i * 60 + seed * 13) % 256, (i * 23) % 256, 255 - i * 15))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def solid(color=(200, 200, 200), size=(200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class Web:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, cap=0):
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError("The site answered with an error (HTTP 404).")
        ctype, body = self.routes[url]
        return url, ctype, body


class Pics:
    """Stands in for deck/userimg: save -> id, has(id)."""

    def __init__(self, existing=()):
        self.saved = {i: b"" for i in existing}

    def save(self, png: bytes) -> str:
        pid = hashlib.sha1(png).hexdigest()
        self.saved[pid] = png
        return pid

    def has(self, pid: str) -> bool:
        return pid in self.saved


def page(og="", title="Vine Boom - Sound Instants", extra=""):
    meta = f'<meta property="og:image" content="{og}">' if og else ""
    return (f"<html><head><title>{title}</title>{meta}</head><body>{extra}"
            f"<div class='btn' onclick=\"play('/media/x.mp3')\"></div></body></html>").encode()


HOME = "https://sounds.example/"


def make(tmp_path, routes, catalog_entries=(), existing=()):
    cat = aw.Catalog(tmp_path / "meme_catalog.json")
    for e in catalog_entries:
        cat.upsert(**e)
    pics = Pics(existing)
    web = Web({HOME: ("text/html", page(og="https://sounds.example/static/site-card.jpg")),
               "https://sounds.example/static/site-card.jpg": ("image/jpeg", picture(99)), **routes})
    art = aw.Artwork(cat, tmp_path / "cache", lambda: web, pics.save, pics.has)
    return art, web, pics, cat


# ---- candidates --------------------------------------------------------------------------------------------

def test_candidates_from_a_page():
    html = """<html><head>
      <meta property="og:image" content="/img/vine-boom.jpg">
      <meta name="twitter:image:src" content="https://cdn.example/tw.png">
      <link rel="image_src" href="https://cdn.example/src.png">
      <script type="application/ld+json">{"@type": "AudioObject", "image": {"url": "https://cdn.example/ld.jpg"}}</script>
      </head><body>
      <div class="related"><img src="/img/other-sound.jpg"></div>
      <div class="instant"><img src="/img/thumb.jpg" alt="">
        <button onclick="play('/media/sounds/vine-boom.mp3')">Play</button></div>
      </body></html>"""
    assert aw.page_candidates(html, "https://sounds.example/en/instant/vine-boom/") == [
        "https://sounds.example/img/vine-boom.jpg", "https://cdn.example/tw.png", "https://cdn.example/ld.jpg",
        "https://cdn.example/src.png", "https://sounds.example/img/thumb.jpg"]   # the img by the play button, not "related"
    assert aw.page_candidates("<p>nothing</p>", "https://e.example/") == []
    assert aw.page_candidates("<meta property='og:image' content='javascript:alert(1)'>", "https://e.example/") == []


def test_generic_names_and_bad_pictures():
    for url in ("https://s.example/static/logo.png", "https://s.example/img/og-default.jpg", "https://s.example/favicon.ico",
                "https://s.example/assets/share-card.png", "https://s.example/apple-touch-icon.png",
                "https://s.example/brand/banner.jpg", "https://s.example/img/placeholder.png"):
        assert aw.url_looks_generic(url), url
    assert not aw.url_looks_generic("https://s.example/media/instants/vine-boom-1234.jpg")
    assert aw.decode(picture(1)).size == (200, 200)
    for data, why in ((picture(1, (60, 60)), "small"), (picture(1, (400, 120)), "shape"), (solid(), "blank"),
                      (b"<html>", "picture"), (b"", "big")):
        with pytest.raises(aw.ArtError, match=why):
            aw.decode(data)
    a, b = aw.decode(picture(3)), aw.decode(picture(4))
    assert aw.hamming(aw.dhash(a), aw.dhash(aw.decode(picture(3, fmt="PNG")))) <= aw.GENERIC_BITS  # same picture
    assert aw.hamming(aw.dhash(a), aw.dhash(b)) > aw.GENERIC_BITS


# ---- the resolver -------------------------------------------------------------------------------------------

def test_a_genuine_page_thumbnail_is_used(tmp_path):
    url = "https://sounds.example/media/instants/vine-boom.jpg"
    art, web, pics, _ = make(tmp_path, {url: ("image/jpeg", picture(1))})
    d = art.resolve("Wah Wah", page=("https://sounds.example/en/instant/vine-boom-1/", page(og=url).decode()))
    assert d["status"] == "auto" and d["assign"]["origin"] == "page" and d["assign"]["src"] == url
    assert pics.has(d["assign"]["image"])


def test_the_sites_own_pictures_are_rejected(tmp_path):
    card = "https://sounds.example/static/site-card.jpg"                       # the homepage's og:image
    copy = "https://cdn.example/uploads/abc123.jpg"                            # the same picture, another address
    logo = "https://sounds.example/static/logo.png"
    art, web, _, _ = make(tmp_path, {copy: ("image/jpeg", picture(99)), logo: ("image/png", picture(5))})
    for og in (card, copy, logo):
        d = art.resolve("Something", page=("https://sounds.example/en/instant/x-1/", page(og=og).decode()))
        assert d["status"] == "none", og
    assert logo not in web.calls                                               # never even downloaded
    assert web.calls.count(HOME) == 1                                          # homepage looked up once per site


def test_a_picture_reused_for_several_sounds_is_generic(tmp_path):
    shared = "https://sounds.example/media/instants/pic-77.jpg"
    art, _, _, _ = make(tmp_path, {shared: ("image/jpeg", picture(7))})
    first = art.resolve("one", page=("https://sounds.example/en/instant/one-1/", page(og=shared).decode()))
    assert first["status"] == "auto"
    second = art.resolve("two", page=("https://sounds.example/en/instant/two-2/", page(og=shared).decode()))
    assert second["status"] == "none"                                          # seen for 2 different sounds
    again = art.resolve("one", page=("https://sounds.example/en/instant/one-1/", page(og=shared).decode()))
    assert again["status"] == "none"                                           # stays generic for that site


def test_catalog_tiers_and_lazy_pictures(tmp_path):
    known = "a" * 40
    lazy = "https://cdn.example/memes/bruh.jpg"
    art, web, pics, cat = make(tmp_path, {lazy: ("image/jpeg", picture(11))}, catalog_entries=[
        {"name": "Vine boom", "aliases": ["vine boom bass"], "image_id": known},
        {"name": "Bruh", "picture_url": lazy},
        {"name": "Emotional damage", "image_id": known}], existing=[known])
    assert [round(s, 2) for s, _ in cat.match(["Vine Boom Sound Effect (HD)"])][:1] == [1.0]   # exact alias
    assert cat.match(["boom vine"])[0][0] == 1.0                                # same words: confident
    assert 0.5 <= cat.match(["vine boom loud remix"])[0][0] < 0.85              # close: uncertain
    assert cat.match(["steel drum"]) == []                                      # nothing
    d = art.resolve("Vine Boom")
    assert d["status"] == "auto" and d["assign"] == {"image": known, "origin": "catalog", "src": "Vine boom"}
    d = art.resolve("bruh", link="")                                            # picture_url: fetched once, kept
    assert d["status"] == "auto" and pics.has(d["assign"]["image"])
    assert aw.Catalog(tmp_path / "meme_catalog.json").entries[1]["image_id"] == d["assign"]["image"]
    n = web.calls.count(lazy)
    art.resolve("bruh")
    assert web.calls.count(lazy) == n


def test_uncertain_matches_are_offered_as_choices(tmp_path):
    lazy = "https://cdn.example/memes/damage.jpg"
    known = "b" * 40
    art, _, pics, _ = make(tmp_path, {lazy: ("image/jpeg", picture(21))}, catalog_entries=[
        {"name": "Emotional damage", "picture_url": lazy}, {"name": "Emotional support", "image_id": known}],
        existing=[known])
    d = art.resolve("emotional damage remix 2")
    assert d["status"] == "choose" and d["assign"] is None and 1 <= len(d["choices"]) <= aw.MAX_CHOICES
    cache = next(c for c in d["choices"] if c["cid"].startswith("cache:"))
    assert art.thumb(cache["cid"])[:4] == b"\x89PNG" and cache["thumb"].endswith(cache["cid"])
    art.remember_choices("f" * 40 + ".mp3", d["choices"])
    assert aw.Artwork(art.catalog, tmp_path / "cache", lambda: None, pics.save, pics.has).choices("f" * 40 + ".mp3")
    image = art.choose(cache["cid"], "f" * 40 + ".mp3")
    assert pics.has(image) and art.choices("f" * 40 + ".mp3") == []
    assert art.choose("icon:" + known) == known
    for bad in ("cache:../../x", "http://evil", "icon:" + "c" * 40):
        with pytest.raises(aw.ArtError):
            art.choose(bad)


# ---- keeping the user's pictures; refreshing automatic ones ----------------------------------------------------

def test_manual_pictures_are_never_replaced():
    found = {"status": "auto", "assign": {"image": "n" * 40, "origin": "page", "src": "https://x/y.jpg"}, "choices": []}
    nothing = {"status": "none", "assign": None, "choices": []}
    manual = {"image": "m" * 40}                                                # the user's hand-picked (no image_auto) 
    assert aw.is_manual(manual) and aw.apply_decision(manual, found) == manual
    kept_symbol = {"image": "", "image_origin": "manual"}
    assert aw.apply_decision(kept_symbol, found) == kept_symbol
    auto = {"image": "o" * 40, "image_auto": True, "image_origin": "page", "image_src": "https://x/old.jpg"}
    assert aw.apply_decision(auto, found)["image"] == "n" * 40                   # replaced with the new sound's
    assert aw.apply_decision(auto, nothing) == {"image": "", "image_auto": False, "image_origin": "", "image_src": ""}
    assert aw.apply_decision({}, found)["image_auto"] is True


def test_seed_import(tmp_path):
    there = "8" * 40
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / "horn.png").write_bytes(picture(31, fmt="PNG"))
    seed = [
        {"slot": "airhorn", "label": "Big Horn: GO", "source": "https://sounds.example/en/instant/big-horn-go-30566/",
         "picture": "https://cdn.example/horn.jpg", "image": there, "icon_file": "icons/horn.png"},
        {"slot": "vineboom", "label": "Wah Wah", "image": "9" * 40, "icon_file": "icons/horn.png"},
        {"slot": "boing", "label": "Clang", "picture": "https://cdn.example/clang.jpg", "image": "7" * 40},
        {"slot": "x", "label": "Escape", "icon_file": "../../secrets.json"},
        {"label": ""},
    ]
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(seed), encoding="utf-8")
    cat = aw.Catalog(tmp_path / "meme_catalog.json")
    imported = []
    r = aw.import_sources(cat, path, lambda i: i == there, lambda data: imported.append(data) or "5" * 40)
    assert r == {"added": 4, "updated": 0, "entries": 4}
    horn, wah, clang, escape = cat.entries
    assert horn["image_id"] == there and "big horn go 30566" in horn["aliases"]    # id reused, slug words as alias
    assert wah["image_id"] == "5" * 40 and len(imported) == 1                      # icon_file imported
    assert clang["image_id"] == "" and clang["picture_url"] == "https://cdn.example/clang.jpg"  # lazy download later
    assert escape["image_id"] == ""                                                # never reads outside the folder
    again = aw.import_sources(aw.Catalog(tmp_path / "meme_catalog.json"), path, lambda i: i == there, lambda d: "5" * 40)
    assert again == {"added": 0, "updated": 4, "entries": 4}                       # idempotent
    assert aw.Catalog(tmp_path / "meme_catalog.json").match(["big horn go"])[0][0] == 1.0
    with pytest.raises(aw.ArtError):
        aw.import_sources(cat, tmp_path / "missing.json", lambda i: False, lambda d: "")


def test_learning_from_the_users_picks(fake_platform):
    from pandadeck.deck import userimg
    from pandadeck.service import Service
    svc = Service(panel=False, platform=fake_platform)
    mine = userimg.save_upload(picture(41, fmt="PNG"))
    auto = userimg.save_upload(picture(42, fmt="PNG"))
    svc.settings.update({"deck": {"sounds": {"airhorn": {"label": "Big Horn", "image": mine, "image_origin": "manual"},
                                             "tada": {"label": "Auto one", "image": auto, "image_auto": True,
                                                      "image_origin": "page"}}}})
    cat = aw.Catalog(svc.artwork.catalog.path)                                    # learned and saved
    assert cat.match(["big horn"])[0][1]["image_id"] == mine
    assert cat.match(["auto one"]) == []                                           # automatic pictures don't teach


def test_artwork_api(fake_platform, monkeypatch):
    import http.client

    from pandadeck.service import Service
    from pandadeck.web import api
    from pandadeck.web.server import make_server
    svc = Service(panel=False, platform=fake_platform)
    decisions = {"Clang": {"status": "auto", "assign": {"image": "e" * 40, "origin": "catalog", "src": "Clang"},
                          "choices": []},
                 "Close": {"status": "choose", "assign": None,
                           "choices": [{"cid": "icon:" + "d" * 40, "thumb": "/api/icons/get?id=" + "d" * 40, "name": "Close"}]}}
    monkeypatch.setattr(svc.artwork, "resolve", lambda label, link="", page=None, title="": decisions.get(
        label, {"status": "none", "assign": None, "choices": []}))
    srv = make_server(svc, 0, api.ROUTES)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **(headers or {"X-PandaDeck": "1"})})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    f = "a" * 40 + ".mp3"
    try:
        st, r = call("POST", "/api/sounds/artwork", {"label": "Clang", "file": f, "entry": {}})
        assert st == 200 and r["entry"] == {"image": "e" * 40, "image_auto": True, "image_origin": "catalog", "image_src": "Clang"}
        st, r = call("POST", "/api/sounds/artwork", {"label": "Clang", "entry": {"image": "m" * 40}})
        assert r["manual"] and r["entry"]["image"] == "m" * 40 and r["entry"]["image_auto"] is False   # yours: kept
        st, r = call("POST", "/api/sounds/artwork", {"label": "Nope", "entry": {"image": "o" * 40, "image_auto": True,
                                                                                  "image_origin": "page"}})
        assert r["entry"]["image"] == "" and r["status"] == "none"            # replaced sound, nothing found: removed
        st, r = call("POST", "/api/sounds/artwork", {"label": "Close", "file": f, "entry": {}})
        assert r["status"] == "choose" and call("GET", "/api/sounds/artwork/choices?file=" + f)[1]["choices"]
        assert f in call("GET", "/api/status")[1]["deck"]["picture_choices"]
        assert call("POST", "/api/sounds/artwork", {"label": "x", "link": "http://evil/"})[0] == 400
        assert call("POST", "/api/sounds/artwork", {"label": "x"}, headers={"X-Other": "1"})[0] == 403  # guarded
        assert call("GET", "/api/sounds/artwork/thumb?cid=cache:" + "0" * 40, headers={"X-Other": "1"})[0] == 404  # unguarded, but only cache files
        assert call("POST", "/api/sounds/artwork/choose", {"cid": "icon:" + "9" * 40})[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_import(tmp_path, capsys):
    from pandadeck.__main__ import main
    path = tmp_path / "s.json"
    path.write_text(json.dumps([{"label": "Bruh", "picture": "https://cdn.example/bruh.jpg"}]), encoding="utf-8")
    assert main(["--import-meme-sources", str(path)]) == 0
    assert "1 added" in capsys.readouterr().out
    assert main(["--import-meme-sources", str(tmp_path / "missing.json")]) == 2
