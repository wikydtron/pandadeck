"""JSON API behind the settings page. Every handler takes (svc, Request) and
returns a dict (200 JSON) or a Response. Secrets go in, never out (the panel
token's "Show token" is the one documented exception)."""
from __future__ import annotations

import logging
import secrets as _secrets
import time
import urllib.parse

from .. import APP_ID, __version__
from ..ai import connect as ai_connect
from ..ai.usage import read_codex_auth
from ..config import SettingsError
from ..deck import userimg
from ..firmware import (FirmwareJob, clean_host, download_release, install_on_stock, is_newer, latest_release,
                        stock_updater_present, update_panel)
from ..link.base import LinkError, Unsupported, check_firmware
from ..music.spotify import SpotifyError
from ..secrets import validate_user_secret
from ..sounds import SoundError
from ..sounds.store import UPLOAD_NAME as SOUND_FILE
from .server import ApiError, Response

log = logging.getLogger("api")

RELEASE_CACHE_S = 600
STAGED_TTL_S = 900
MAX_STAGED = 3
BOOTLOADER_PAUSE_S = 600


def _obj(req) -> dict:
    body = req.json()
    if not isinstance(body, dict):
        raise ApiError(400, "expected a JSON object")
    return body


# ---- basics ------------------------------------------------------------------------------

def ping(svc, req):
    return {"app": APP_ID, "version": __version__, "port": svc.port}


def status(svc, req):
    return svc.status()


def get_settings(svc, req):
    return {"settings": svc.settings.get(), "secrets": svc.secrets.presence(), "meta": svc.meta()}


def extensions(svc, req):
    return svc.extensions_public()


def post_settings(svc, req):
    try:
        new = svc.settings.update(_obj(req), check=getattr(svc, "check_settings", None))
    except SettingsError as e:
        raise ApiError(400, str(e)) from None
    return {"ok": True, "settings": new}


def icon_upload(svc, req):
    """Raw picture body -> stored, normalized PNG; returns its id."""
    try:
        image_id = userimg.save_upload(req.body)
    except userimg.ImageError as e:
        raise ApiError(400, str(e)) from None
    return {"ok": True, "id": image_id}


def icon_get(svc, req):
    data = userimg.png_bytes(req.arg("id"))
    if not data:
        raise ApiError(404, "no such picture")
    return Response(200, data, "image/png", {"Cache-Control": "private, max-age=86400"})


def post_secret(svc, req):
    body = _obj(req)
    name = str(body.get("name") or "")
    try:
        value = validate_user_secret(name, body.get("value", ""))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    svc.secrets.set(name, value or None)
    svc.secret_changed(name)
    return {"ok": True, "secrets": svc.secrets.presence()}


# ---- sounds ------------------------------------------------------------------------------------

def sound_upload(svc, req):
    """Raw WAV / MP3 / OGG body (?name=<original file name>) -> stored as <sha1>.<ext>."""
    try:
        name = svc.sounds.store.save_upload(req.body, req.arg("name") or "")
    except SoundError as e:
        raise ApiError(400, str(e)) from None
    return {"ok": True, "file": name}


def sound_play(svc, req):
    """Play a sound on this computer (the settings page's preview button)."""
    from ..deck.grammar import SOUND_ID
    body = _obj(req)
    sid, file = str(body.get("id") or ""), str(body.get("file") or "")
    if sid and not SOUND_ID.fullmatch(sid) or file and not SOUND_FILE.fullmatch(file) or not (sid or file):
        raise ApiError(400, "unknown sound")
    vol = body.get("volume")
    if vol is not None and (isinstance(vol, bool) or not isinstance(vol, int) or not 0 <= vol <= 100):
        raise ApiError(400, "volume is 0 to 100")
    if not svc.sounds.preview(sid, file, vol, default=bool(body.get("default"))):
        raise ApiError(404, "no such sound")
    return {"ok": True}


def sound_fetch(svc, req):
    """POST {"url"}: import a sound from a link (in the background); GET: how it's going."""
    from ..sounds.fetch import FetchError, check_url
    if req.method == "GET":
        return svc.sound_import.status()
    url = str(_obj(req).get("url") or "").strip()
    try:
        check_url(url)
        svc.sound_import.start(url)
    except FetchError as e:
        raise ApiError(400 if "Already" not in str(e) else 409, str(e)) from None
    return svc.sound_import.status()


def sound_artwork(svc, req):
    """Look for a picture for a sound being imported / replaced.
    {"label", "link"?, "file"?, "entry": {image, image_auto, image_origin, image_src}}
    -> {"status", "entry": the picture fields to save, "choices": [...]}"""
    from ..sounds import artwork
    body = _obj(req)
    label = str(body.get("label") or "")[:120]
    link = str(body.get("link") or "")[:1024]
    file = str(body.get("file") or "")
    entry = body.get("entry") if isinstance(body.get("entry"), dict) else {}
    if link and not link.startswith("https://"):
        raise ApiError(400, "links start with https://")
    if file and not SOUND_FILE.fullmatch(file):
        raise ApiError(400, "unknown sound file")
    page = svc.sound_import.pages.get(link) if link else None
    decision = svc.artwork.resolve(label, link=link, page=page if page and page[1] else None)
    if file:
        svc.artwork.remember_choices(file, decision["choices"])
    keep = ("image", "image_auto", "image_origin", "image_src")
    out = artwork.apply_decision({k: entry.get(k) for k in keep}, decision)
    return {"status": decision["status"], "entry": {k: out.get(k) or ("" if k != "image_auto" else False) for k in keep},
            "choices": decision["choices"], "manual": artwork.is_manual(entry)}


def sound_artwork_choices(svc, req):
    file = req.arg("file")
    if not SOUND_FILE.fullmatch(file or ""):
        raise ApiError(400, "unknown sound file")
    return {"choices": svc.artwork.choices(file)}


def sound_artwork_choose(svc, req):
    from ..sounds.artwork import ArtError
    body = _obj(req)
    file = str(body.get("file") or "")
    try:
        image = svc.artwork.choose(str(body.get("cid") or ""), file if SOUND_FILE.fullmatch(file) else "")
    except ArtError as e:
        raise ApiError(404, str(e)) from None
    return {"ok": True, "image": image}


def sound_artwork_thumb(svc, req):
    data = svc.artwork.thumb(req.arg("cid"))
    if not data:
        raise ApiError(404, "no picture")
    return Response(200, data, "image/png", {"Cache-Control": "private, max-age=86400"})


def sound_artwork_import(svc, req):
    """Sounds card -> "Import picture mappings...": a sources.json picked with the native picker."""
    from .. import picker
    from ..sounds.artwork import ArtError
    try:
        path = picker.pick("file")
    except picker.PickerBusy as e:
        raise ApiError(409, str(e)) from None
    except picker.PickerUnavailable:
        raise ApiError(501, "No file picker on this system: use  pandadeck --import-meme-sources PATH") from None
    if not path:
        return {"cancelled": True}
    try:
        return {"ok": True, **svc.import_meme_sources(path)}
    except ArtError as e:
        raise ApiError(400, str(e)) from None


def ai_icons(svc, req):
    """GET: the current ✨ Generate with AI run. POST {engine, info, style}: start one."""
    from ..ai.icons import IconError
    if req.method == "GET":
        return {**svc.ai_icons.status(), **svc.ai_icons_meta()}
    body = _obj(req)
    raw = body.get("info") if isinstance(body.get("info"), dict) else {}
    info = {k: str(raw.get(k) or "")[:120] for k in ("label", "kind", "sound", "app", "color", "page_title")}
    link = str(raw.get("link") or "")
    page = svc.sound_import.pages.get(link) if link else None
    if page and page[1] and not info["page_title"]:
        from ..sounds.fetch import page_title
        info["page_title"] = page_title(page[1])[:120]
    engine = str(body.get("engine") or "")
    if engine == "claude" and not svc.ai_icons_meta()["renderer"]:
        raise ApiError(501, "Claude's icons need an SVG renderer: pip install \"pandadeck[ai-icons]\"")
    try:
        reference = svc.ai_reference({**info, "image": str(raw.get("image") or ""),
                                      "image_origin": str(raw.get("image_origin") or "")}) if engine == "codex" else None
        svc.ai_icons.start(engine, info, str(body.get("style") or "")[:80], reference=reference)
    except IconError as e:
        raise ApiError(409 if "Already" in str(e) else 400, str(e)) from None
    return svc.ai_icons.status()


def ai_icons_thumb(svc, req):
    data = svc.ai_icons.png(req.arg("cid"))
    if not data:
        raise ApiError(404, "no picture")
    return Response(200, data, "image/png", {"Cache-Control": "private, max-age=3600"})


def ai_icons_choose(svc, req):
    """The picked AI picture -> PandaDeck's icon store (a picture of the user's own choosing)."""
    data = svc.ai_icons.png(str(_obj(req).get("cid") or ""))
    if not data:
        raise ApiError(404, "That picture is gone: generate again.")
    try:
        return {"ok": True, "image": userimg.save_upload(data)}
    except userimg.ImageError as e:
        raise ApiError(400, str(e)) from None


def sound_stop(svc, req):
    svc.sounds.stop_all()
    return {"ok": True}


# ---- apps / deck ------------------------------------------------------------------------------

def apps(svc, req):
    # 54 = three app grids' worth, so "Fill" on an extra app page continues with the next-ranked apps
    return {"ready": svc.board.catalog_ready(), "apps": svc.board.catalog(), "suggested": svc.board.suggested(54),
            "error": svc.board.error}


def browse(svc, req):
    """Open a native file/folder picker on this computer; returns the chosen path."""
    from .. import picker
    from ..osutil import runs_code
    kind = _obj(req).get("kind")
    if kind not in ("file", "folder"):
        raise ApiError(400, "kind is file or folder")
    try:
        path = picker.pick(kind)
    except picker.PickerBusy as e:
        raise ApiError(409, str(e)) from None
    except picker.PickerUnavailable:
        raise ApiError(501, "No file picker on this system: type or paste the full path instead") from None
    if not path:
        return {"cancelled": True}
    return {"path": path, "kind": kind, "runs_code": kind == "file" and runs_code(path)}


def target_icon(svc, req):
    data = svc.board.target_png(req.arg("id"))
    if not data:
        raise ApiError(404, "no icon")
    return Response(200, data, "image/png", {"Cache-Control": "private, max-age=600"})


def apps_refresh(svc, req):
    svc.board.refresh()
    return {"ok": True}


def app_icon(svc, req):
    app_id = req.arg("id")
    if not app_id or len(app_id) > 512:
        raise ApiError(400, "missing id")
    if not svc.board.known(app_id):
        raise ApiError(404, "unknown app")
    data = svc.board.icon_png(app_id)
    if not data:
        raise ApiError(404, "no icon")
    return Response(200, data, "image/png", {"Cache-Control": "private, max-age=3600"})


def deck_preview(svc, req):
    return {"deck": svc.board.current()}


def music_like(svc, req):
    """❤ Like / unlike the playing track (same as the panel's heart)."""
    if not svc.likes.available():
        raise ApiError(409, "Sign in with Spotify (again) to use Like")
    if not svc.likes.toggle():
        raise ApiError(409, "This track isn't on Spotify, or its state isn't known yet")
    svc.media.poke()
    return {"ok": True}


def music_cover(svc, req):
    data = svc.media.cover_png() if svc.media else None
    if not data:
        raise ApiError(404, "nothing playing")
    return Response(200, data, "image/png")


# ---- panel --------------------------------------------------------------------------------------

def _link(svc):
    link = svc.links.current()
    if link is None:
        raise ApiError(409, "No panel connected")
    if svc.links.busy:
        raise ApiError(409, "The panel is busy updating")
    return link


def _config(svc, payload: dict) -> dict:
    link = _link(svc)
    try:
        out = link.config(payload)
    except Unsupported:
        raise ApiError(501, "Your panel's firmware is too old for this. Update it first.") from None
    except LinkError as e:
        raise ApiError(502, f"The panel didn't answer: {e}") from None
    if isinstance(out.get("settings"), dict):
        svc.links.update_info(settings=out["settings"])
    if out.get("ok") is False:
        raise ApiError(400, str(out.get("error") or "the panel refused it"))
    return out


def panel_config(svc, req):
    body = _obj(req)
    payload = {}
    if "brightness" in body:
        b = body["brightness"]
        if isinstance(b, bool) or not isinstance(b, (int, float)) or not 5 <= b <= 100:
            raise ApiError(400, "brightness must be 5..100")
        payload["brightness"] = int(b)
    for k in ("flip", "tflip"):
        if k in body:
            if not isinstance(body[k], bool):
                raise ApiError(400, f"{k} must be true or false")
            payload[k] = body[k]
    if "name" in body:
        name = str(body["name"]).strip()
        if not 0 < len(name) <= 24 or not name.isascii() or not name.isprintable():
            raise ApiError(400, "name: 1-24 plain letters/digits")
        payload["name"] = name
    if not payload:
        raise ApiError(400, "nothing to change")
    return _config(svc, payload)


def panel_wifi(svc, req):
    body = _obj(req)
    ssid, pw = body.get("ssid", ""), body.get("pass", "")
    if not isinstance(ssid, str) or not isinstance(pw, str) or len(ssid) > 32 or len(pw) > 63:
        raise ApiError(400, "network name up to 32 characters, password up to 63")
    if pw and len(pw) < 8:
        raise ApiError(400, "Wi-Fi passwords are at least 8 characters")
    link = _link(svc)
    if link.kind != "usb":
        raise ApiError(409, "Wi-Fi details are only sent over the USB cable: plug the panel in first")
    return _config(svc, {"wifi": {"ssid": ssid, "pass": pw}})


def panel_reset(svc, req):
    return _config(svc, {"reset": True})


def panel_bootloader(svc, req):
    _link(svc)
    out = _config(svc, {"bootloader": True})
    svc.links.pause_usb(BOOTLOADER_PAUSE_S)  # leave the port to esptool
    svc.links.reconnect()
    return out


def panel_token(svc, req):
    tok = svc.secrets.get("panel_token") or ""
    if not tok:
        raise ApiError(404, "No token yet: plug the panel in by USB once")
    return {"token": tok}


def panel_reconnect(svc, req):
    svc.links.resume_usb()
    svc.links.reconnect()
    return {"ok": True}


def wifi_ssids(svc, req):
    return {"ssids": svc.wifi_suggestions()}


# ---- firmware -------------------------------------------------------------------------------------

def firmware_latest(svc, req):
    cache = svc.release_cache
    if req.arg("force") != "1" and cache and time.time() - cache[0] < RELEASE_CACHE_S:
        rel = cache[1]
    else:
        try:
            rel = latest_release()
        except (LinkError, OSError) as e:
            return {"ok": False, "error": str(e)}
        svc.release_cache = (time.time(), rel)
    current = (svc.links.status().get("info") or {}).get("fw", "")
    return {"ok": True, **rel, "current": current, "newer": bool(current) and is_newer(rel["version"], current)}


def firmware_file(svc, req):
    """Raw .bin body -> validated + staged; the page then asks where to flash it."""
    try:
        info = check_firmware(req.body)
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    sid = _secrets.token_hex(8)
    fresh = sorted(((k, v) for k, v in svc.staged.items() if time.time() - v[0] < STAGED_TTL_S),
                   key=lambda kv: kv[1][0])[-(MAX_STAGED - 1):]
    svc.staged = {**dict(fresh), sid: (time.time(), req.body)}
    return {"ok": True, "staged": sid, "info": info}


def _staged_or_latest(svc, body: dict, job: FirmwareJob) -> bytes:
    if body.get("source") == "latest":
        job._set(state="downloading", message="Downloading the latest firmware")
        return download_release(latest_release())
    sid = str(body.get("staged") or "")
    item = svc.staged.get(sid)
    if not item:
        raise ValueError("that file expired: pick it again")
    return item[1]


def firmware_update(svc, req):
    body = _obj(req)
    if svc.links.current() is None:
        raise ApiError(409, "No panel connected")
    job = svc.job

    def work():
        data = _staged_or_latest(svc, body, job)
        update_panel(job, svc.links, svc.secrets, svc.state, data)

    if not job.start("panel", work):
        raise ApiError(409, "A firmware update is already running")
    return {"ok": True}


def install_check(svc, req):
    body = _obj(req)
    try:
        host = clean_host(str(body.get("host") or ""))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return {"host": host, "updater": stock_updater_present(host)}


def install_start(svc, req):
    body = _obj(req)
    try:
        host = clean_host(str(body.get("host") or ""))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    job = svc.job

    def work():
        if not stock_updater_present(host):
            raise LinkError(f"No firmware updater at http://{host}/update. Check the address on the panel.")
        data = _staged_or_latest(svc, body, job)
        install_on_stock(job, host, data)

    if not job.start("install", work):
        raise ApiError(409, "A firmware upload is already running")
    return {"ok": True}


def firmware_job(svc, req):
    return svc.job.status()


# ---- music ---------------------------------------------------------------------------------------------

def spotify_login(svc, req):
    try:
        url = svc.spotify_auth.begin(svc.redirect_uri())
    except SpotifyError as e:
        raise ApiError(400, str(e)) from None
    return {"url": url}


def spotify_logout(svc, req):
    svc.spotify_auth.sign_out()
    svc.state.set("spotify_name", None)
    return {"ok": True}


def spotify_callback(svc, req):
    code, state, err = req.arg("code"), req.arg("state"), req.arg("error")
    result = "ok"
    if err:
        result = "denied" if err == "access_denied" else "error"
    else:
        try:
            svc.spotify_auth.finish(code, state)
            svc.spotify_signed_in()
        except SpotifyError as e:
            log.info("spotify sign-in failed: %s", e)
            svc.spotify_auth.last_result = str(e)
            result = "error"
    q = urllib.parse.urlencode({"spotify": result})
    return Response(303, b"", "text/plain", {"Location": f"/?{q}#music"})


# ---- AI --------------------------------------------------------------------------------------------------

def claude_connect(svc, req):
    return ai_connect.start_setup_token()


def claude_token(svc, req):
    body = _obj(req)
    try:
        value = validate_user_secret("claude_token", body.get("token", ""))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    if value and not value.startswith("sk-ant-"):
        raise ApiError(400, "That isn't a Claude token (it starts with sk-ant-)")
    svc.secrets.set("claude_token", value or None)
    svc.settings.update({"ai": {"claude_usage": "token" if value else "off"}})
    svc.secret_changed("claude_token")
    return {"ok": True}


def codex_connect(svc, req):
    found = read_codex_auth() is not None
    if found:
        svc.settings.update({"ai": {"codex_usage": "auth"}})
        svc.usage.poke()
    return {"found": found}


# ---- system ----------------------------------------------------------------------------------------------

def autostart(svc, req):
    body = _obj(req)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(400, "enabled must be true or false")
    try:
        svc.set_autostart(enabled)
    except Exception as e:  # noqa: BLE001 - platform errors shown to the user
        raise ApiError(500, f"Couldn't change the login item: {e}") from None
    return {"ok": True, "enabled": svc.autostart_enabled()}


def open_folder(svc, req):
    what = _obj(req).get("what")
    if what not in ("config", "logs"):
        raise ApiError(400, "unknown folder")
    svc.open_folder(what)
    return {"ok": True}


ROUTES = {
    ("GET", "/api/ping"): ping,
    ("GET", "/api/status"): status,
    ("GET", "/api/settings"): get_settings,
    ("POST", "/api/settings"): post_settings,
    ("GET", "/api/extensions"): extensions,
    ("POST", "/api/icons"): icon_upload,
    ("POST", "/api/browse"): browse,
    ("GET", "/api/targets/icon"): target_icon,
    ("GET", "/api/icons/get"): icon_get,
    ("POST", "/api/secret"): post_secret,
    ("GET", "/api/apps"): apps,
    ("POST", "/api/apps/refresh"): apps_refresh,
    ("GET", "/api/apps/icon"): app_icon,
    ("GET", "/api/deck"): deck_preview,
    ("GET", "/api/music/cover"): music_cover,
    ("POST", "/api/music/like"): music_like,
    ("POST", "/api/panel/config"): panel_config,
    ("POST", "/api/panel/wifi"): panel_wifi,
    ("POST", "/api/panel/reset"): panel_reset,
    ("POST", "/api/panel/bootloader"): panel_bootloader,
    ("POST", "/api/panel/token"): panel_token,
    ("POST", "/api/panel/reconnect"): panel_reconnect,
    ("GET", "/api/wifi/ssids"): wifi_ssids,
    ("GET", "/api/firmware/latest"): firmware_latest,
    ("POST", "/api/firmware/file"): firmware_file,
    ("POST", "/api/firmware/update"): firmware_update,
    ("GET", "/api/firmware/job"): firmware_job,
    ("POST", "/api/install/check"): install_check,
    ("POST", "/api/install/start"): install_start,
    ("POST", "/api/spotify/login"): spotify_login,
    ("POST", "/api/spotify/logout"): spotify_logout,
    ("GET", "/spotify/callback"): spotify_callback,
    ("POST", "/api/claude/connect"): claude_connect,
    ("POST", "/api/claude/token"): claude_token,
    ("POST", "/api/codex/connect"): codex_connect,
    ("POST", "/api/autostart"): autostart,
    ("POST", "/api/open"): open_folder,
    ("POST", "/api/sounds/upload"): sound_upload,
    ("POST", "/api/sounds/play"): sound_play,
    ("POST", "/api/sounds/stop"): sound_stop,
    ("POST", "/api/sounds/fetch"): sound_fetch,
    ("GET", "/api/sounds/fetch"): sound_fetch,
    ("POST", "/api/sounds/artwork"): sound_artwork,
    ("GET", "/api/sounds/artwork/choices"): sound_artwork_choices,
    ("POST", "/api/sounds/artwork/choose"): sound_artwork_choose,
    ("GET", "/api/sounds/artwork/thumb"): sound_artwork_thumb,
    ("POST", "/api/sounds/artwork/import"): sound_artwork_import,
    ("GET", "/api/ai/icons"): ai_icons,
    ("POST", "/api/ai/icons"): ai_icons,
    ("GET", "/api/ai/icons/thumb"): ai_icons_thumb,
    ("POST", "/api/ai/icons/choose"): ai_icons_choose,
}
UPLOAD_PATHS = ("/api/firmware/file", "/api/icons", "/api/sounds/upload")
