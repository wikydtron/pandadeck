"""Shared fixtures for the desktop app tests (tests/app). The app package lives
in app/pandadeck; every test runs with its own settings folder and fake home."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from PIL import Image  # noqa: E402

from pandadeck.platform.base import AppInfo, NowPlaying  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real settings folder, ~/.claude or ~/.codex."""
    home = tmp_path / "pd-home"
    user = tmp_path / "user-home"
    home.mkdir()
    user.mkdir()
    monkeypatch.setenv("PANDADECK_HOME", str(home))
    monkeypatch.setenv("PANDADECK_USER_HOME", str(user))
    # Never start the real Claude / Codex CLIs ("Generate with AI"): tests inject fake engines and runs.
    from pandadeck.ai import icons
    monkeypatch.setattr(icons, "find_engines", lambda: {})
    return home


@pytest.fixture
def user_home(tmp_path):
    return tmp_path / "user-home"


class FakeAudio:
    def __init__(self):
        self.muted, self.toggles = False, 0

    def volume(self):
        return 42

    def mic_muted(self):
        return self.muted

    def toggle_mic(self):
        self.toggles += 1
        self.muted = not self.muted


class FakeMedia:
    def __init__(self, np: NowPlaying | None = None):
        self.np, self.cmds = np, []

    def now_playing(self):
        return self.np

    def command(self, cmd):
        self.cmds.append(cmd)


class FakePlatform:
    name = "windows"

    def __init__(self, apps=None):
        self.apps = apps if apps is not None else [
            AppInfo("id.code", "Visual Studio Code", 80.0),
            AppInfo("id.chrome", "Google Chrome", 50.0),
            AppInfo("id.calc", "Calculator", 0.0),
            AppInfo("id.notes", "Notes for Windows", 3.0),
        ]
        self.launched: list[str] = []
        self.auto: tuple | None = None
        self.audio_backend = FakeAudio()
        self.media_backend = None

    def list_apps(self):
        return list(self.apps)

    def app_icon(self, app_id):
        if app_id == "id.calc":
            return None
        img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        img.paste(Image.new("RGBA", (64, 64), (200, 30, 30, 255)), (32, 32))
        return img

    def launch_app(self, app_id):
        if app_id not in {a.id for a in self.apps}:
            raise ValueError("unknown app")
        self.launched.append(app_id)

    def media(self):
        return self.media_backend

    def audio(self):
        return self.audio_backend

    def set_autostart(self, enabled, argv):
        self.auto = (enabled, list(argv))

    def autostart_enabled(self):
        return bool(self.auto and self.auto[0])


@pytest.fixture
def fake_platform():
    return FakePlatform()


class FakeSettings:
    """Minimal stand-in for config.Settings: section() reads a plain dict."""

    def __init__(self, data: dict | None = None):
        from pandadeck.config import clean
        self.data = clean(data or {})
        self.first_run = False

    def get(self):
        return self.data

    def section(self, name):
        return self.data.get(name)

    def update(self, patch):
        from pandadeck.config import apply_patch
        self.data = apply_patch(self.data, patch)
        return self.data

    def on_change(self, fn):
        pass


class FakeSecrets(dict):
    def get(self, k, d=None):  # noqa: D401
        return super().get(k, d)

    def set(self, k, v):
        if v in (None, "", {}):
            self.pop(k, None)
        else:
            self[k] = v

    def clear(self, k):  # noqa: D401
        self.pop(k, None)

    def has(self, k):
        return bool(super().get(k))


class FakeState(dict):
    def get(self, k, d=None):  # noqa: D401
        return super().get(k, d)

    def set(self, k, v):
        if v is None:
            self.pop(k, None)
        else:
            self[k] = v


@pytest.fixture
def settings():
    return FakeSettings()


@pytest.fixture
def make_settings():
    return FakeSettings


@pytest.fixture
def fake_secrets():
    return FakeSecrets()


@pytest.fixture
def fake_state():
    return FakeState()
