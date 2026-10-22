"""Deck round 2: padded suggestions, tile overrides, uploaded pictures, files /
folders / websites, app-grid pages, the icon budget, the native picker, and
artist photos without Spotify."""
import io
import json
import os
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from pandadeck import osutil, picker
from pandadeck.config import SettingsError, apply_patch, clean
from pandadeck.deck import board as bd
from pandadeck.deck import favicon, userimg
from pandadeck.deck import grammar as g
from pandadeck.music import artist as art
from pandadeck.platform.base import AppInfo, NowPlaying


def png(size=(64, 64), color=(200, 30, 30, 255), fmt="PNG", mode="RGBA"):
    buf = io.BytesIO()
    img = Image.new(mode, size, color if mode == "RGBA" else color[:3])
    img.save(buf, fmt)
    return buf.getvalue()


# ---- suggestions ------------------------------------------------------------------------------

def test_suggestions_pad_to_18_with_well_known_apps():
    used = [AppInfo(f"u{i}", f"Used {i}", 10 - i) for i in range(9)]  # Windows often knows only ~9
    rest = [AppInfo("chrome", "Google Chrome", 0), AppInfo("term", "Terminal", 0), AppInfo("zz", "Zebra Tool", 0),
            AppInfo("un", "Uninstall Foo", 0), AppInfo("vlc2", "VLC media player - reset preferences", 0),
            AppInfo("spot", "Spotify", 0), AppInfo("calc", "Calculator", 0)]
    rest += [AppInfo(f"x{i}", f"App {i:02d}", 0) for i in range(20)]
    out = [a.id for a in bd.rank_suggestions(used + rest)]
    assert len(out) == 18 and out[:9] == [f"u{i}" for i in range(9)]
    assert out[9:13] == ["chrome", "term", "spot", "calc"]   # well-known, in their usual order
    assert "un" not in out and "vlc2" not in out               # not apps you'd put on a deck
    assert out[13] == "x0"                                     # then alphabetical
    assert len(bd.rank_suggestions(used[:3])) == 3             # never invents apps


# ---- settings validation ----------------------------------------------------------------------

def test_targets_actions_layouts_and_overrides_validate():
    s = apply_patch(clean({}), {"deck": {
        "targets": {"t1": {"kind": "url", "url": "https://github.com", "label": "GitHub"},
                    "t2": {"kind": "folder", "path": "C:/Users", "label": ""}},
        "apps": ["Chrome", "target:t1", "target:gone"],
        "app_overrides": {"Chrome": {"label": "Chr\u00f4me", "color": "#60a5fa", "image": ""}},
        "custom": [{"id": "c1", "name": "grid", "layout": "apps",
                    "buttons": [{"label": "Docs", "action": "target:t2", "image": "a" * 40}] * 18}]}})
    d = s["deck"]
    assert d["apps"] == ["Chrome", "target:t1"]  # references to missing targets are dropped
    assert d["app_overrides"]["Chrome"] == {"label": "Chrome", "image": "", "color": "#60A5FA"}
    assert d["custom"][0]["layout"] == "apps" and len(d["custom"][0]["buttons"]) == 18
    for bad in ({"targets": {"t1": {"kind": "url", "url": "javascript:alert(1)"}}},
                {"targets": {"t1": {"kind": "file", "path": ""}}},
                {"targets": {"bad id": {"kind": "url", "url": "https://x"}}},
                {"custom": [{"id": "c1", "layout": "keys", "buttons": [{"action": "keys:A"}] * 13}]},
                {"custom": [{"id": "c1", "buttons": [{"action": "target:Not-an-id"}]}]},
                {"custom": [{"id": "c1", "buttons": [{"action": "keys:A", "image": "../../etc/passwd"}]}]},
                {"app_overrides": {"x": {"color": "red"}}}):
        with pytest.raises(SettingsError):
            apply_patch(clean({}), {"deck": bad})
    assert g.validate_action("target:t1") == "target:t1"


# ---- uploaded pictures -----------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "BMP", "WEBP", "ICO"])
def test_upload_formats_are_normalized(fmt):
    mode = "RGB" if fmt in ("JPEG", "BMP") else "RGBA"
    image_id = userimg.save_upload(png((128, 96), fmt=fmt, mode=mode))
    assert len(image_id) == 40 and userimg.path_for(image_id).exists()
    assert userimg.png_bytes(image_id)[:4] == b"\x89PNG"
    assert len(userimg.panel_bytes(image_id)) == 56 * 56 * 3
    assert userimg.save_upload(png((128, 96), fmt=fmt, mode=mode)) == image_id  # same picture, same id


def test_upload_is_trimmed_and_bounded():
    img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (300, 100), (10, 200, 10, 255)), (50, 150))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    stored = userimg.load(userimg.save_upload(buf.getvalue()))
    assert stored.size == (256, 256)  # trimmed to the content (squared), then capped at 256


def test_bad_uploads_are_refused():
    with pytest.raises(userimg.ImageError, match="2 MB"):
        userimg.save_upload(b"\0" * (2 * 1024 * 1024 + 1))
    with pytest.raises(userimg.ImageError):
        userimg.save_upload(b"definitely not an image")
    gif = io.BytesIO()
    Image.new("RGB", (8, 8)).save(gif, "GIF")
    with pytest.raises(userimg.ImageError, match="PNG"):
        userimg.save_upload(gif.getvalue())
    bomb = io.BytesIO()
    Image.new("L", (5000, 5000)).save(bomb, "PNG")  # tiny file, 25 megapixels decoded
    assert len(bomb.getvalue()) < 2 * 1024 * 1024
    with pytest.raises(userimg.ImageError, match="too large"):
        userimg.save_upload(bomb.getvalue())
    assert userimg.path_for("../../secrets.json") is None and userimg.load("nope") is None


# ---- the board: tiles, targets, pictures, layouts, budget -------------------------------------

def test_board_tiles_targets_pictures_and_grid_pages(settings, fake_platform, monkeypatch, tmp_path):
    pic = userimg.save_upload(png())
    folder = tmp_path / "Projects"
    folder.mkdir()
    fake_platform.file_icon = lambda path: Image.new("RGBA", (64, 64), (0, 0, 255, 255))
    monkeypatch.setattr(favicon, "fetch", lambda url, cache=None: Image.new("RGBA", (32, 32), (0, 0, 0, 255)))
    settings.update({"deck": {
        "pages": ["apps", "c1"],
        "targets": {"t1": {"kind": "url", "url": "https://www.github.com/", "label": ""},
                    "t2": {"kind": "folder", "path": str(folder), "label": ""}},
        "apps": ["id.chrome", "target:t1", "id.code"],
        "app_overrides": {"id.chrome": {"label": "Chrome", "color": "#60A5FA", "image": ""},
                          "id.code": {"label": "", "color": "", "image": pic}},
        "custom": [{"id": "c1", "name": "MINE", "layout": "apps", "buttons": [
            {"label": "", "icon": "", "color": "", "image": "", "action": "target:t2"},
            {"label": "Pic", "icon": "", "color": "", "image": pic, "action": "keys:F13"},
            {"label": "Hub", "icon": "", "color": "", "image": "", "action": "target:t1"}]}]}})
    b = bd.DeckBoard(settings, fake_platform, "windows")
    b._load_catalog()
    apps, mine = b.current()["pages"]
    assert [t["label"] for t in apps["buttons"]] == ["Chrome", "github.com", "Visual Studio Code"]
    assert apps["buttons"][0]["color"] == "#60A5FA"
    assert apps["buttons"][1] == {"label": "github.com", "icon": "web", "img": "a1", "action": "agent:launch:target:t1"}
    assert mine["cols"] == 6
    assert mine["buttons"][0]["action"] == "agent:launch:target:t2" and mine["buttons"][0]["label"] == "Projects"
    folder_key = mine["buttons"][0]["img"]
    assert mine["buttons"][0]["icon"] == "folder" and folder_key.startswith("t")
    assert mine["buttons"][1]["img"] == "u0" and mine["buttons"][2]["img"] == "a1"  # shared with the APPS tile
    assert {"icon:a0", "icon:a1", "icon:a2", "icon:u0", "icon:" + folder_key} <= set(b.images())
    assert all(len(json.dumps(btn["action"])) <= 49 for p in b.current()["pages"] for btn in p["buttons"])


def test_icon_budget():
    deck = {"pages": ["apps", "c1"], "apps": [f"a{i}" for i in range(18)], "app_overrides": {}, "targets": {},
            "custom": [{"id": "c1", "buttons": [{"action": f"app:b{i}", "image": ""} for i in range(18)]}]}
    assert bd.icon_demand(deck) == 36
    deck["custom"][0]["buttons"] += [{"action": "keys:A", "image": f"{i:040x}"} for i in range(12)]
    assert bd.icon_demand(deck) == 48 > bd.ICON_SLOTS


def test_settings_refuse_more_pictures_than_the_panel_holds(fake_platform):
    from pandadeck.service import Service
    fake_platform.apps = [AppInfo(f"app{i}", f"App {i}", 1) for i in range(60)]
    svc = Service(panel=False, platform=fake_platform)
    svc.board._load_catalog()
    grid = lambda n, start: [{"label": "", "icon": "", "color": "", "image": "", "action": f"app:app{start + i}"}  # noqa: E731
                             for i in range(n)]
    ok = {"pages": ["apps", "c1", "c2"], "apps": [f"app{i}" for i in range(18)],
          "custom": [{"id": "c1", "name": "A", "layout": "apps", "buttons": grid(18, 18)},
                     {"id": "c2", "name": "B", "layout": "apps", "buttons": grid(18, 36)}]}  # 54 pictures
    with pytest.raises(SettingsError, match="47 button pictures"):
        svc.settings.update({"deck": ok}, check=svc.check_settings)
    ok["custom"][1]["buttons"] = grid(18, 0)  # the same apps as the APPS page: 36 pictures
    svc.settings.update({"deck": ok}, check=svc.check_settings)


def test_new_file_targets_must_exist(fake_platform, tmp_path, monkeypatch):
    from pandadeck.service import Service
    svc = Service(panel=False, platform=fake_platform)
    real = tmp_path / "run.bat"
    real.write_text("echo hi")
    with pytest.raises(SettingsError, match="doesn't exist"):
        svc.settings.update({"deck": {"targets": {"t1": {"kind": "file", "path": str(tmp_path / "nope.bat")}}}},
                            check=svc.check_settings)
    with pytest.raises(SettingsError):
        svc.settings.update({"deck": {"targets": {"t1": {"kind": "folder", "path": str(real)}}}},
                            check=svc.check_settings)
    svc.settings.update({"deck": {"targets": {"t1": {"kind": "file", "path": f'"{real}"'}}, "apps": ["target:t1"]}},
                        check=svc.check_settings)
    assert svc.settings.section("deck")["targets"]["t1"]["path"] == str(real)  # "Copy as path" quotes removed
    assert osutil.runs_code(str(real)) and not osutil.runs_code("notes.txt")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    svc.settings.update({"deck": {"targets": {"t2": {"kind": "file", "path": "~/run.bat"}}}}, check=svc.check_settings)
    opened = []
    monkeypatch.setattr(osutil.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(osutil.subprocess, "Popen", lambda argv, **kw: opened.append(argv[-1]))
    assert osutil.open_target(svc.settings.section("deck")["targets"]["t2"])
    assert [os.path.normpath(o) for o in opened] == [str(real)]


# ---- launching ---------------------------------------------------------------------------------

def test_open_target_per_os(monkeypatch, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    calls = []
    monkeypatch.setattr(osutil.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(osutil.os, "startfile", lambda p: calls.append(["startfile", p]), raising=False)
    monkeypatch.setattr(osutil.webbrowser, "open", lambda u: calls.append(["browser", u]) or True)
    for plat, expect in (("win32", ["startfile", str(f)]), ("darwin", ["open", str(f)]), ("linux", ["xdg-open", str(f)])):
        monkeypatch.setattr(osutil.sys, "platform", plat)
        assert osutil.open_target({"kind": "file", "path": str(f)})
        assert calls[-1] == expect
    assert not osutil.open_target({"kind": "file", "path": str(tmp_path / "gone.txt")})
    assert osutil.open_target({"kind": "url", "url": "https://example.com"}) and calls[-1] == ["browser", "https://example.com"]
    assert not osutil.open_target({"kind": "url", "url": "file:///etc/passwd"})


def test_panel_event_launches_a_target(settings, fake_platform, monkeypatch):
    from pandadeck.actions import Actions
    opened = []
    monkeypatch.setattr(osutil, "open_target", lambda t: opened.append(t) or True)
    settings.update({"deck": {"targets": {"t1": {"kind": "url", "url": "https://x.org"}}, "apps": ["target:t1"]}})
    b = bd.DeckBoard(settings, fake_platform, "windows")
    Actions(board=b).run({"k": "cmd", "a": "launch:target:t1"})
    Actions(board=b).run({"k": "cmd", "a": "launch:target:nope"})
    assert opened == [{"kind": "url", "url": "https://x.org", "label": ""}]


# ---- native picker ----------------------------------------------------------------------------

def test_picker_dispatch_and_single_dialog(monkeypatch):
    runs = []

    def fake_run(argv, **kw):
        runs.append(argv)
        return SimpleNamespace(returncode=0, stdout="/Users/me/Music/\n")

    monkeypatch.setattr(picker.subprocess, "run", fake_run)
    monkeypatch.setattr(picker.shutil, "which", lambda n: "/usr/bin/" + n)
    assert picker.pick("folder", platform="darwin") == "/Users/me/Music"
    assert runs[-1][0] == "osascript" and "choose folder" in runs[-1][2]
    monkeypatch.setenv("DISPLAY", ":0")
    assert picker.pick("file", platform="linux") == "/Users/me/Music/"
    assert runs[-1][:2] == ["zenity", "--file-selection"]
    with pytest.raises(ValueError):
        picker.pick("anything")
    picker._busy.acquire()
    try:
        with pytest.raises(picker.PickerBusy):
            picker.pick("file", platform="darwin")
    finally:
        picker._busy.release()
    monkeypatch.setattr(picker.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout=""))
    assert picker.pick("file", platform="darwin") is None  # cancelled


def test_browse_api_is_guarded_and_reports(fake_platform, monkeypatch):
    import http.client

    from pandadeck.service import Service
    from pandadeck.web import api
    from pandadeck.web.server import make_server
    svc = Service(panel=False, platform=fake_platform)
    srv = make_server(svc, 0, api.ROUTES)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(body, headers):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/api/browse", body=json.dumps(body), headers={"Host": f"127.0.0.1:{port}",
                                                                          "Content-Type": "application/json", **headers})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null")
        c.close()
        return out

    try:
        monkeypatch.setattr(picker, "pick", lambda kind: "C:/tools/start.bat")
        assert post({"kind": "file"}, {})[0] == 403                      # no X-PandaDeck: refused
        assert post({"kind": "file"}, {"X-PandaDeck": "1", "Origin": "http://evil.example"})[0] == 403
        status, body = post({"kind": "file"}, {"X-PandaDeck": "1"})
        assert status == 200 and body == {"path": "C:/tools/start.bat", "kind": "file", "runs_code": True}
        monkeypatch.setattr(picker, "pick", lambda kind: None)
        assert post({"kind": "folder"}, {"X-PandaDeck": "1"})[1] == {"cancelled": True}

        def unavailable(kind):
            raise picker.PickerUnavailable("no tk")
        monkeypatch.setattr(picker, "pick", unavailable)
        assert post({"kind": "file"}, {"X-PandaDeck": "1"})[0] == 501
        assert post({"kind": "disk"}, {"X-PandaDeck": "1"})[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()


# ---- website icons ---------------------------------------------------------------------------

def test_icon_links_and_favicon_fetch(tmp_path):
    html = ('<html><head><link rel="icon" href="/favicon-16.png" sizes="16x16">'
            '<link rel="apple-touch-icon" href="https://cdn.example/touch.png">'
            '<link rel="icon" href="http://insecure.example/x.png"></head></html>')
    assert favicon.icon_links(html, "https://example.com/") == ["https://cdn.example/touch.png",
                                                               "https://example.com/favicon-16.png"]
    calls = []

    def req(url, **kw):
        calls.append(url)
        if url == "https://example.com/":
            return 200, {}, html.encode()
        if url == "https://cdn.example/touch.png":
            return 200, {}, png((180, 180))
        return 404, {}, b""

    img = favicon.fetch("https://example.com/page", tmp_path, request=req)
    assert img.size == (180, 180) and calls[:2] == ["https://example.com/", "https://cdn.example/touch.png"]
    n = len(calls)
    assert favicon.fetch("https://example.com/other", tmp_path, request=req).size == (180, 180)
    assert len(calls) == n  # cached per host


def test_bounded_downloads(monkeypatch):
    from pandadeck import util

    class Resp(io.BytesIO):
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(util, "_opener", lambda url, follow: SimpleNamespace(open=lambda req, timeout: Resp(b"x" * 100)))
    assert util.http_request("https://x", max_bytes=100)[2] == b"x" * 100
    with pytest.raises(OSError, match="larger"):
        util.http_request("https://x", max_bytes=99)


# ---- artist photos without Spotify --------------------------------------------------------------

def test_primary_artist_and_hosts():
    assert art.primary_artist("Calvin Harris, Dua Lipa") == "Calvin Harris"
    assert art.primary_artist("Drake feat. Rihanna") == "Drake" and art.primary_artist("A ft. B") == "A"
    assert art.primary_artist("Simon & Garfunkel") == "Simon & Garfunkel" and art.primary_artist("") == ""
    assert art.image_url_ok("https://e-cdns-images.dzcdn.net/images/artist/x/1000x1000.jpg")
    for bad in ("http://e-cdns-images.dzcdn.net/x.jpg", "https://evil.com/x.jpg", "https://dzcdn.net.evil.com/x.jpg"):
        assert not art.image_url_ok(bad)
    assert art.pick_picture({"data": [{"picture_xl": "https://evil.com/a.jpg",
                                       "picture_big": "https://cdn-images.dzcdn.net/b.jpg"}]}) == \
        "https://cdn-images.dzcdn.net/b.jpg"
    assert art.pick_picture({"data": []}) == "" and art.pick_picture(None) == ""


def test_artist_lookup_caches_hits_and_misses(tmp_path):
    jpg = io.BytesIO()
    Image.new("RGB", (500, 500), (200, 100, 50)).save(jpg, "JPEG")
    calls = []

    def req(url, **kw):
        calls.append(url)
        assert kw.get("follow_redirects") is False and kw.get("max_bytes")
        if url.startswith(art.SEARCH_URL):
            if "Nobody" in url:
                return 200, {}, b'{"data": []}'
            return 200, {}, json.dumps({"data": [{"name": "Glass Harbor",
                                                  "picture_xl": "https://e-cdns-images.dzcdn.net/a.jpg"}]}).encode()
        return 200, {}, jpg.getvalue()

    a = art.ArtistImages(tmp_path, request=req)
    assert a.get_blocking("Glass Harbor feat. Paper Satellites").size == (500, 500)
    assert "q=Glass+Harbor" in calls[0] and len(calls) == 2
    assert a.get("Glass Harbor").size == (500, 500) and len(calls) == 2      # memory cache
    b = art.ArtistImages(tmp_path, request=req)
    assert b.get_blocking("glass harbor").size == (500, 500) and len(calls) == 2  # disk cache (24 h)
    assert a.get_blocking("Nobody Known") is None
    n = len(calls)
    assert art.ArtistImages(tmp_path, request=req).get_blocking("Nobody Known") is None and len(calls) == n


def test_collector_uses_deezer_when_spotify_has_no_photo(settings):
    from pandadeck.music.collector import MediaCollector
    settings.update({"music": {"backdrop": "artist"}})
    photo = Image.new("RGB", (64, 64), (250, 250, 250))

    class Artists:
        def get(self, name):
            return photo if name == "Glass Harbor" else None

    np = NowPlaying(app="Chrome", title="Lantern Light", artist="Glass Harbor", playing=True,
                    art=Image.new("RGB", (64, 64), (10, 10, 200)), art_key="k")

    class Sys:
        def now_playing(self):
            return np

        def command(self, c):
            pass

    col = MediaCollector(settings, lambda: Sys(), lambda: None, artists=Artists())
    col.step()
    assert col.backdrop_source == "deezer"
    with_photo = col.images()["backdrop"][1]
    np.artist = "Unknown"
    col.step()
    assert col.backdrop_source == "cover" and col.images()["backdrop"][1] != with_photo


def test_picker_windows_uses_a_hidden_topmost_tk_root(monkeypatch):
    import sys
    seen = {}

    class Root:
        def withdraw(self):
            seen["withdrawn"] = True

        def attributes(self, *a):
            seen["attrs"] = a

        def update(self):
            pass

        def destroy(self):
            seen["destroyed"] = True

    dialogs = SimpleNamespace(askopenfilename=lambda **kw: "C:/Games/run.exe", askdirectory=lambda **kw: "")
    fake_tk = SimpleNamespace(Tk=Root, filedialog=dialogs)
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", dialogs)
    assert picker.pick("file", platform="win32").replace("\\", "/") == "C:/Games/run.exe"
    assert seen == {"withdrawn": True, "attrs": ("-topmost", True), "destroyed": True}
    assert picker.pick("folder", platform="win32") is None  # cancelled

    def broken():
        raise RuntimeError("no display")
    monkeypatch.setattr(fake_tk, "Tk", broken)
    with pytest.raises(picker.PickerUnavailable):
        picker.pick("file", platform="win32")


def test_dark_logos_get_a_light_plate():
    logo = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    logo.paste(Image.new("RGBA", (40, 40), (20, 20, 20, 255)), (12, 12))   # black logo, transparent around
    out = favicon.readable(logo)
    assert out.size == (84, 84) and out.getpixel((42, 4))[:3] == favicon.PLATE[:3]
    assert out.getpixel((42, 42))[:3] == (20, 20, 20)
    light = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    light.paste(Image.new("RGBA", (40, 40), (90, 160, 250, 255)), (12, 12))
    assert favicon.readable(light).size == (64, 64)                          # colourful: unchanged
    tile = Image.new("RGBA", (64, 64), (10, 10, 10, 255))
    assert favicon.readable(tile).size == (64, 64)                           # has its own background


def test_artist_photo_needs_exact_name_and_skips_deezer_placeholder():
    from pandadeck.music import artist as art
    data = {"data": [
        {"name": "Lumen Craft", "picture_xl": "https://cdn-images.dzcdn.net/images/artist/abc/1000x1000.jpg"},
        {"name": "Lumen Coast", "picture_xl": "https://cdn-images.dzcdn.net/images/artist//1000x1000-000000-80-0-0.jpg"},
    ]}
    assert art.pick_picture(data, "Lumen Coast") == ""        # right artist, but only the placeholder
    assert art.pick_picture(data, "lumen craft") != ""        # exact (normalized) name match
    assert art.pick_picture(data, "Nobody") == ""             # never someone else's photo
    good = {"data": [{"name": "The Paper Satellites",
                      "picture_xl": "https://cdn-images.dzcdn.net/images/artist/f00/1000x1000.jpg"}]}
    assert art.pick_picture(good, "Paper Satellites") != ""   # "The" and case ignored
    assert art.is_placeholder("https://cdn-images.dzcdn.net/images/artist//500x500.jpg")


def test_artist_photo_prefers_most_followed_and_skips_empty_md5():
    from pandadeck.music import artist as art
    data = {"data": [
        {"name": "Northern Static", "nb_fan": 12,
         "picture_xl": "https://cdn-images.dzcdn.net/images/artist/d41d8cd98f00b204e9800998ecf8427e/1000x1000.jpg"},
        {"name": "Northern Static", "nb_fan": 48210,
         "picture_xl": "https://cdn-images.dzcdn.net/images/artist/7e4a2b9c0d/1000x1000.jpg"},
    ]}
    assert "7e4a2b9c0d" in art.pick_picture(data, "Northern Static")
    only_placeholder = {"data": [data["data"][0]]}
    assert art.pick_picture(only_placeholder, "Northern Static") == ""


# ---- button style: classic cards or the icon as the whole button --------------------------------------

def test_button_style_settings_and_sizes():
    from pandadeck.music import images as im
    s = clean({})
    assert s["deck"]["style"] == "classic" and s["deck"]["labels"] is True          # today's look stays the default
    with pytest.raises(SettingsError):
        apply_patch(s, {"deck": {"style": "huge"}})
    with pytest.raises(SettingsError):
        apply_patch(s, {"deck": {"labels": "yes"}})
    assert bd.deck_style({}) == ("classic", True, im.ICON_PX)
    assert bd.deck_style({"style": "classic", "labels": False}) == ("classic", True, im.ICON_PX)  # classic always named
    assert bd.deck_style({"style": "icons"}) == ("icons", True, 80)
    assert bd.deck_style({"style": "icons", "labels": False}) == ("icons", False, 96)


def test_icons_style_sends_bigger_icons_and_a_new_rev(settings, fake_platform):
    pic = userimg.save_upload(png())
    settings.update({"deck": {"pages": ["apps", "c1"], "apps": ["id.chrome", "id.code"],
                              "custom": [{"id": "c1", "name": "MINE", "buttons": [
                                  {"label": "Pic", "icon": "", "color": "", "image": pic, "action": "keys:F13"}]}]}})
    b = bd.DeckBoard(settings, fake_platform, "windows")
    b._load_catalog()
    classic = b.current()
    assert classic["style"] == "classic" and classic["labels"] is True
    assert classic["rev"] == bd.deck_rev(classic["pages"])                         # unchanged for existing decks
    sizes = {name: (hdr["w"], len(data)) for name, (hdr, data) in b.images().items()}
    assert sizes["icon:a0"] == (56, 56 * 56 * 3) and sizes["icon:u0"] == (56, 56 * 56 * 3)
    for labels, px in ((True, 80), (False, 96)):
        settings.update({"deck": {"style": "icons", "labels": labels}})
        b._rebuild()
        deck = b.current()
        assert deck["style"] == "icons" and deck["labels"] is labels and deck["rev"] != classic["rev"]
        imgs = b.images()
        assert {hdr["w"] for hdr, _ in imgs.values()} == {px}
        assert all(len(data) == px * px * 3 for _, data in imgs.values())
        assert len(imgs) == len(sizes)                                             # same icons, same budget
    assert len(userimg.panel_bytes(pic, 96)) == 96 * 96 * 3
