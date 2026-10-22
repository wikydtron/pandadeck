"""Telling which web player a browser plays: window titles (Windows), page
address (Linux MPRIS), or the signed-in Spotify account; the panel's source
dot colour."""
from pandadeck.music.collector import app_label, snapshot_block, source_label
from pandadeck.platform import _web_players as wp
from pandadeck.platform.base import NowPlaying

B = "\u2022"


def test_browsers_are_recognised():
    for app, src in (("Google Chrome", "Chrome"), ("Microsoft Edge", "MSEdge"), ("Chrome", "Chrome"),
                     ("Brave", "Brave.ABC123"), ("Opera GX Browser", ""), ("Vivaldi", ""), ("Firefox", "308046B0AF4A39CB"),
                     ("Arc", "TheBrowserCompany.Arc_ttt!Arc"), ("Media", "Chromium.QWERTYUIOPASDF"),
                     ("Chromium", "org.mpris.MediaPlayer2.chromium.instance42")):
        assert wp.is_browser(app, src), (app, src)
    for app, src in (("Spotify", "Spotify.exe"), ("Media Player", "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic"),
                     ("VLC", "VideoLAN.VLC"), ("Search", ""), ("Apple Music", "AppleInc.AppleMusicWin")):
        assert not wp.is_browser(app, src), (app, src)


def test_spotify_web_tab_title():
    titles = ["Inbox - Outlook", f"Out Of The Blue Hour {B} Northern Static - Google Chrome", "Desktop"]
    assert wp.service_from_titles(titles, "Out Of The Blue Hour", "Northern Static") == "Spotify"
    assert wp.service_from_titles([f"Out Of The  Blue Hour {B} Northern Static, Someone - Brave"], "out of the blue hour",
                                  "Northern Static, Someone") == "Spotify"
    assert wp.service_from_titles(titles, "Out Of The Blue Hour", "Other Artist") == ""  # a different song
    assert wp.service_from_titles(titles, "Blue Hour", "Northern Static") == ""          # must be the whole title
    assert wp.service_from_titles(titles, "", "") == "" and wp.service_from_titles([], "x", "y") == ""


def test_spotify_desktop_window_shows_the_web_players_track():
    # Spotify's desktop app titles its window "<artist> - <title>" for whatever the account
    # plays, even on another device such as the web player in a background browser tab.
    windows = ["Inbox - Outlook", "VËLVET-TIDE - Copper Skies"]
    assert wp.service_from_titles(windows, "Copper Skies", "VELVET-TIDE") == "Spotify"
    assert wp.service_from_titles(windows, "Copper Skies", "VELVET-TIDE, Paper Satellites, The-Guest") == "Spotify"
    assert wp.service_from_titles(["VELVET-TIDE - Copper Skies - VLC media player"], "Copper Skies",
                                  "VELVET-TIDE") == ""  # another player's window isn't Spotify


def test_other_web_players_by_marker():
    cases = {
        "(3) Lofi beats to relax - YouTube - Google Chrome": "YouTube",
        "Lofi beats to relax - YouTube Music - Microsoft\u200b Edge": "YouTube Music",
        "Stream Lofi beats to relax by DJ | Listen online for free on SoundCloud - Firefox": "SoundCloud",
        "Lofi beats to relax by DJ on Apple Music - Google Chrome": "Apple Music",
        "Lofi beats to relax - DJ | Deezer - Opera": "Deezer",
        "Lofi beats to relax by DJ on TIDAL - Vivaldi": "TIDAL",
    }
    for window, service in cases.items():
        assert wp.service_from_titles([window], "Lofi beats to relax", "DJ") == service, window
    # the marker must follow the track title; a search page about it isn't the player
    assert wp.service_from_titles(["YouTube - Lofi beats to relax - Google Search"], "Lofi beats to relax") == ""
    assert wp.service_from_titles(["Lofi beats to relax - Google Search"], "Lofi beats to relax") == ""


def test_page_address_from_mpris():
    assert wp.service_from_url("https://open.spotify.com/track/abc") == "Spotify"
    assert wp.service_from_url("https://music.youtube.com/watch?v=1") == "YouTube Music"
    assert wp.service_from_url("https://www.youtube.com/watch?v=1") == "YouTube"
    assert wp.service_from_url("https://youtu.be/x") == "YouTube"
    assert wp.service_from_url("https://listen.tidal.com/album/1") == "TIDAL"
    assert wp.service_from_url("https://www.deezer.com/fr/") == "Deezer"
    for bad in ("https://notyoutube.com/x", "file:///tmp/youtube.com.mp3", "", "https://evil.com/?open.spotify.com"):
        assert wp.service_from_url(bad) == ""


def test_labels_are_cached_per_track():
    now = [0.0]
    calls = []
    windows = [["Chrome"]]

    def titles():
        calls.append(1)
        return windows[0]

    labels = wp.WebPlayerLabels(titles, clock=lambda: now[0])
    assert labels.label("Spotify", "Spotify.exe", "Song", "Band") == "Spotify" and calls == []  # not a browser
    assert labels.label("Google Chrome", "Chrome", "Song", "Band") == "Google Chrome" and len(calls) == 1
    now[0] = 1.0
    assert labels.label("Google Chrome", "Chrome", "Song", "Band") == "Google Chrome" and len(calls) == 1  # cached miss
    windows[0] = [f"Song {B} Band - Google Chrome"]
    now[0] = 1.0 + wp.RECHECK_S
    assert labels.label("Google Chrome", "Chrome", "Song", "Band") == "Spotify" and len(calls) == 2
    windows[0] = []
    now[0] = 100.0
    assert labels.label("Google Chrome", "Chrome", "Song", "Band") == "Spotify" and len(calls) == 2  # found: sticks
    assert labels.label("Google Chrome", "Chrome", "Next song", "Band") == "Google Chrome" and len(calls) == 3
    assert labels.label("Firefox", "", "Song", "", url="https://www.youtube.com/watch?v=1") == "YouTube"

    def broken():
        raise OSError("no desktop")
    assert wp.WebPlayerLabels(broken).label("Brave", "", "Song", "Band") == "Brave"


def test_snapshot_source_name_and_colour():
    np = NowPlaying(app="Google Chrome", title="Song", artist="Band", playing=True, extra={"source": "Chrome"})
    sp = NowPlaying(app="Spotify", title="song", artist="Band", playing=True)
    assert source_label(np, sp) == "Spotify"            # the signed-in account plays this very track
    assert source_label(np, NowPlaying(app="Spotify", title="Other")) == "Chrome" and source_label(np) == "Chrome"
    b = snapshot_block(np, "tag", "#123456", source_label(np, sp))
    assert b["app"] == "Spotify" and b["app_color"] == "#1DB954"
    assert snapshot_block(NowPlaying(app="YouTube Music", title="x"), "", "")["app_color"] == "#FF0033"
    assert app_label("YouTube Music") == "YouTube Music" and app_label("TIDAL") == "TIDAL"
    assert snapshot_block(NowPlaying(app="VLC", title="x"), "", "")["app_color"] == ""   # firmware uses the tint
    assert wp.service_color("SoundCloud") == "#FF5500" and wp.service_color("Apple Music") == "#FA2D48"
    assert wp.service_color("Deezer") == "#A238FF" and wp.service_color("TIDAL") == "#FFFFFF"


def test_mpris_reports_the_web_player(monkeypatch):
    from pandadeck.platform import _mpris as mp
    st = mp.map_player({"PlaybackStatus": "Playing", "Metadata": {"xesam:title": "Song",
                                                                  "xesam:url": "https://open.spotify.com/track/1"}},
                       "Chromium")
    assert st["url"] == "https://open.spotify.com/track/1"
    line = mp.SEP.join(["chromium.instance9", "chromium", "Playing", "Song", "Band", "", "", "", "", "", "",
                        "https://music.youtube.com/watch?v=1"])
    out = mp.parse_playerctl(line)
    assert out["chromium.instance9"]["url"] == "https://music.youtube.com/watch?v=1"

    class Bus:
        def players(self):
            return ["org.mpris.MediaPlayer2.chromium.instance9"]

        def status(self, name):
            return {**st, "app": "Chromium"}

    media = mp.MprisMedia(Bus(), None)
    monkeypatch.setattr(media, "_update_art", lambda url: None)
    assert media.now_playing().app == "Spotify"
