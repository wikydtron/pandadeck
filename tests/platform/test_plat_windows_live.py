"""Read-only smoke checks of the real Windows backend.

Only on a Windows desktop (skipped on other OSes and in CI, where runners
have no Start menu/media session/sound card). Nothing is launched, no volume
or mic state is changed, autostart is not touched.
"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32" or bool(os.environ.get("CI")),
                                reason="needs an interactive Windows desktop")


@pytest.fixture(scope="module")
def plat():
    from pandadeck.platform import current
    return current()


def test_current_is_windows(plat):
    assert plat.name == "windows" and type(plat).__name__ == "WindowsPlatform"


def test_list_apps_finds_start_menu_apps(plat):
    apps = plat.list_apps()
    assert len(apps) > 10
    assert all(a.id and a.name for a in apps)
    assert apps == sorted(apps, key=lambda a: (-a.score, a.name.lower()))


def test_app_icon_renders_a_real_icon(plat):
    apps = plat.list_apps()
    calc = next((a for a in apps if "calculator" in a.name.lower()), apps[0])
    img = plat.app_icon(calc.id)
    assert img is not None and img.mode == "RGBA" and min(img.size) >= 128
    assert img.getchannel("A").getextrema()[1] == 255       # something visible in it
    assert plat.app_icon("Not.A.Real.App!Nope") is None


def test_media_reads_without_crashing(plat):
    media = plat.media()
    if media is None:
        pytest.skip("winrt media packages not installed")
    np = media.now_playing()
    assert np is None or (isinstance(np.title, str) and np.pos >= 0 and np.dur >= 0)
    if np is not None and np.art is not None:
        assert np.art_key and max(np.art.size) <= 640


def test_audio_reports_a_volume(plat):
    audio = plat.audio()
    if audio is None:
        pytest.skip("pycaw not installed")
    deadline = time.time() + 5
    while audio.volume() < 0 and time.time() < deadline:
        time.sleep(0.2)
    assert 0 <= audio.volume() <= 100
    assert audio.mic_muted() in (True, False, None)
