# PandaDeck desktop app — architecture

One Python app for **Windows, macOS and Linux**. It runs in the background
(tray icon), finds the panel by itself (USB first, then Wi-Fi), and serves a
**settings page** at `http://127.0.0.1:47321/` that the tray menu opens.
Everything is optional except the panel link: someone who never codes with
AI just gets DECK / MUSIC / HUD.

Wire format to the panel: see [protocol.md](protocol.md).

## Package layout

```
app/
  pyproject.toml            name "pandadeck", entry point `pandadeck = pandadeck.__main__:main`
  pandadeck/
    __main__.py             CLI: (no args) run service + tray; --settings open the page;
                            --headless no tray; --version
    paths.py                per-OS config/cache/log dirs (no extra deps)
    config.py               settings.json load/save (atomic, 0600), defaults, schema migration
    secrets.py              tokens (spotify, claude) in secrets.json (0600), never sent to the UI
    service.py              wires everything; start/stop; reload on settings change
    link/
      base.py               Link protocol: hello() sync(snap) image(hdr, data) config(d) update(bin)
      usb.py                CDC serial link (pyserial), frame codec, port discovery (VID 0x303A)
      wifi.py               HTTP link with token; mDNS/IP
      discover.py           pick USB if present, else Wi-Fi; reconnect loop
    sync.py                 snapshot assembly, event ack/dedupe, image (re)upload per panel boot
    actions.py              panel events -> media cmds / app launch / mic / perm decisions
    web/
      server.py             ThreadingHTTPServer on 127.0.0.1:47321: settings UI + JSON API +
                            Claude/Codex hook endpoints + Spotify OAuth callback
      static/index.html, app.js, style.css   no build step, no CDN
    ai/
      bridge.py             sessions/perms state
      summarize.py
      hooks.py              install/remove Claude Code + Codex hooks (toggle in UI)
      usage.py              Claude (setup-token -> rate-limit headers) + Codex (~/.codex/auth.json
                            -> chatgpt.com/backend-api/wham/usage); "url" mode for a remote daemon
    music/
      collector.py          polls the active MediaBackend; cover -> art + backdrop; tint
      images.py             art_image, backdrop_image, encode_rgb565(a8), tint_from (pure)
      spotify.py            Spotify Web API backend (PKCE, user's own Client ID), artist image
    deck/
      board.py              deck pages from settings: APPS (chosen apps + icons) + built-ins
      defaults.json         MEDIA / WINDOWS(per OS) / DEV / MACROS pages
    hud/
      pc.py                 psutil (+ nvidia-smi if present): cpu, ram, gpu, temps, disk, net
      lab.py                optional homelab: a `pandadeck-lab` server URL
    platform/
      base.py               interfaces below
      windows.py            GSMTC media, UserAssist + Start-menu apps + shell icons, Core Audio
      macos.py              Spotify/Music via osascript, /Applications + .icns, osascript volume
      linux.py              MPRIS (jeepney/D-Bus or playerctl), .desktop apps + icon theme, pactl
      __init__.py           current() -> Platform for sys.platform
  lab_server/pandadeck_lab.py   tiny optional homelab agent (psutil + docker CLI), token auth
  packaging/                PyInstaller spec + per-OS notes
```

## Platform interface (`platform/base.py`)

```python
@dataclass
class AppInfo:
    id: str            # stable id (AUMID / bundle id / .desktop id)
    name: str
    score: float = 0   # how much the user uses it (for "suggested apps")

@dataclass
class NowPlaying:
    app: str; title: str; artist: str; album: str
    playing: bool; pos: float; dur: float
    shuffle: int = -1; repeat: int = -1      # -1 = not reported
    art: "PIL.Image.Image | None" = None     # cover, any size
    art_key: str = ""                        # changes when the cover changes
    artist_image: "PIL.Image.Image | None" = None   # Spotify only

class MediaBackend(Protocol):
    def now_playing(self) -> NowPlaying | None: ...
    def command(self, cmd: str) -> None: ...  # playpause|next|prev|seek:<s>|shuffle|repeat

class AudioBackend(Protocol):
    def volume(self) -> int: ...              # 0..100, -1 unknown
    def mic_muted(self) -> bool | None: ...
    def toggle_mic(self) -> None: ...

class Platform(Protocol):
    name: str                                 # "windows" | "macos" | "linux"
    def list_apps(self) -> list[AppInfo]: ...
    def app_icon(self, app_id: str) -> "PIL.Image.Image | None": ...   # RGBA
    def launch_app(self, app_id: str) -> None: ...
    def media(self) -> MediaBackend | None: ...
    def audio(self) -> AudioBackend | None: ...
    def set_autostart(self, enabled: bool, argv: list[str]) -> None: ...
    def autostart_enabled(self) -> bool: ...
```

Platform code must import its OS-only modules lazily, so every module
imports cleanly on every OS (tests run on all three).

## Settings (settings.json)

```json
{"version": 1,
 "panel": {"link": "auto", "host": "", "token": ""},
 "pages": ["deck", "music", "hud"],
 "deck": {"apps": ["<app id>", ...], "pages": ["apps", "media", "windows"]},
 "music": {"source": "auto", "spotify_client_id": "", "backdrop": "cover"},
 "ai": {"claude_hooks": false, "codex_hooks": false, "usage": "off",
        "claude_usage_url": "", "codex_usage_url": ""},
 "hud": {"lab_url": "", "lab_token": ""},
 "startup": true}
```

`music.backdrop`: `cover` (blurred cover) or `artist` (Spotify artist photo
when signed in, falls back to cover).

## Settings page

Single page, sections: **Panel** (status, link, Wi-Fi, brightness, flip,
firmware update), **Pages** (toggles + order), **Deck** (pick apps from the
discovered list with icons, choose extra pages), **Music** (source, Spotify
sign-in, backdrop style), **AI** (Claude/Codex hooks, usage sign-in),
**HUD** (PC is automatic; optional homelab URL), **Startup**.

Security: the server binds 127.0.0.1 only, rejects requests whose `Host` is
not `127.0.0.1:47321`/`localhost:47321`, and state-changing API calls need
the `X-PandaDeck: 1` header and an `Origin` (if present) of the page itself.
Secrets are write-only from the page (it can set or clear them, never read
them back).

