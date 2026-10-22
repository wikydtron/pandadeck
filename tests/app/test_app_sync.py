"""Sync loop: snapshot schema, event ack/dedupe, deck resend, image re-upload per boot."""
import json
import time
from pathlib import Path

from pandadeck.actions import Actions
from pandadeck.ai.bridge import Bridge
from pandadeck.link.base import LinkError
from pandadeck.sync import DECK_EVERY, ImageUploader, Syncer

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "snapshot.json"


class Recorder:
    def __init__(self):
        self.seen = []

    def run(self, ev):
        self.seen.append(ev["seq"])


class FakeLink:
    kind, where = "usb", "COM5"

    def __init__(self):
        self.replies = []
        self.sent = []
        self.images = []
        self.fail = None

    def sync(self, snap):
        self.sent.append(snap)
        if self.fail:
            raise self.fail
        return self.replies.pop(0) if self.replies else {"ok": True, "boot": 1, "icons": 0, "events": []}

    def image(self, hdr, data):
        self.images.append((hdr.get("key") or hdr["kind"], len(data)))
        return True


class Links:
    def __init__(self, link):
        self.link, self.busy, self.fails, self.oks = link, "", [], 0

    def current(self):
        return self.link

    def failed(self, e):
        self.fails.append(e)

    def succeeded(self):
        self.oks += 1


def test_events_are_deduped_by_seq_and_reset_on_reboot():
    rec = Recorder()
    s = Syncer(Links(FakeLink()), Bridge("Desk"), rec)
    evs = [{"seq": 1, "k": "cmd", "a": "mic"}, {"seq": 2, "k": "cmd", "a": "mic"}, "junk", {"seq": "x"}]
    s.handle_events(111, evs)
    s.handle_events(111, evs)  # re-sent until acked: must not replay
    snap = s.snapshot()
    assert rec.seen == [1, 2] and snap["ack"] == 2 and snap["boot"] == 111
    s.handle_events(222, [{"seq": 1, "k": "cmd", "a": "mic"}])  # panel rebooted: seq restarts
    assert rec.seen == [1, 2, 1]


def test_once_round_trip_acks_and_runs_actions():
    link = FakeLink()
    link.replies = [{"ok": True, "boot": 5, "icons": 0, "events": [{"seq": 1, "k": "perm", "id": "p1", "a": "allow"}]}]
    bridge = Bridge("Desk")
    calls = []
    bridge.decide = lambda pid, v: calls.append((pid, v)) or True
    links = Links(link)
    s = Syncer(links, bridge, Actions(bridge=bridge))
    assert s.once() and calls == [("p1", "allow")] and links.oks == 1 and s.panel_alive()
    s.once()
    assert link.sent[-1]["ack"] == 1 and link.sent[-1]["boot"] == 5


def test_link_errors_are_reported_and_busy_links_skipped():
    link = FakeLink()
    links = Links(link)
    s = Syncer(links, Bridge("Desk"), Recorder())
    link.fail = LinkError("timeout")
    assert not s.once() and len(links.fails) == 1
    links.busy = "update"
    link.fail = None
    assert not s.once() and link.sent and len(link.sent) == 1  # no sync while flashing
    links.link = None
    assert not s.once()


def test_snapshot_matches_firmware_fixture_schema():
    fixture = json.loads(FIXTURE.read_text())
    b = Bridge("Desk")
    b.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "abc", "cwd": "/x/PandaDeck", "prompt": "go"})
    s = Syncer(Links(FakeLink()), b, Recorder(), sources={"pc": lambda: fixture["pc"], "lab": lambda: fixture["lab"],
                                                           "usage": lambda: fixture["usage"], "media": lambda: None,
                                                           "broken": lambda: 1 / 0})
    snap = s.snapshot()
    assert set(snap) >= {"t", "sessions", "perms", "pc", "lab", "usage", "ack", "boot"}
    assert "media" not in snap and "broken" not in snap
    assert set(snap["sessions"][0]) == set(fixture["sessions"][0])


def test_deck_sent_after_boot_change_rev_change_and_periodically():
    deck = {"rev": 1, "pages": []}
    link = FakeLink()
    s = Syncer(Links(link), Bridge("Desk"), Recorder(), deck=lambda: deck)
    s.once()
    assert "deck" in link.sent[0]
    s.once()
    assert "deck" not in link.sent[1]
    deck = {"rev": 2, "pages": []}
    s.once()
    assert "deck" in link.sent[2]
    link.replies = [{"ok": True, "boot": 9, "events": []}]
    s.once()  # learns the new boot...
    s.once()  # ...and resends
    assert "deck" in link.sent[4]
    for _ in range(DECK_EVERY):
        s.once()
    assert sum("deck" in x for x in link.sent[5:]) >= 1
    s.new_link()
    s.once()
    assert "deck" in link.sent[-1]


def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_image_uploader_once_per_boot_and_content():
    link = FakeLink()
    images = {"icon:a0": ({"kind": "icon", "key": "a0", "w": 1, "h": 1, "tag": ""}, b"abc"),
              "art": ({"kind": "icon", "key": "art", "w": 1, "h": 1, "tag": "t"}, b"art1"),
              "backdrop": ({"kind": "backdrop", "w": 1, "h": 1, "tag": "t"}, b"bg")}
    up = ImageUploader(lambda: link, lambda: dict(images))
    assert up.pending() == []  # no boot known yet
    up.start()
    try:
        up.offer(7, 0)
        assert wait_for(lambda: len(link.images) == 3)
        assert [n for n, _ in link.images][:2] == ["art", "backdrop"]  # what's on screen first
        up.offer(7, 2)
        time.sleep(0.2)
        assert len(link.images) == 3  # nothing new
        images["art"] = ({"kind": "icon", "key": "art", "w": 1, "h": 1, "tag": "u"}, b"art2")
        up.offer(7, 2)
        assert wait_for(lambda: len(link.images) == 4) and link.images[-1][0] == "art"
        up.offer(7, 0)  # the panel lost its icons
        assert wait_for(lambda: len(link.images) == 6)
        up.offer(8, 0)  # reboot: everything again
        assert wait_for(lambda: len(link.images) == 9)
    finally:
        up.stop()


def test_image_uploader_backs_off_when_refused():
    class Refusing(FakeLink):
        def image(self, hdr, data):
            self.images.append(hdr["kind"])
            return False

    link = Refusing()
    up = ImageUploader(lambda: link, lambda: {"art": ({"kind": "icon", "key": "art", "w": 1, "h": 1}, b"x")})
    up.start()
    try:
        up.offer(1, 0)
        assert wait_for(lambda: link.images)
        up.offer(1, 0)
        time.sleep(0.2)
        assert len(link.images) == 1  # waiting 30 s before retrying
    finally:
        up.stop()


def test_image_uploader_resends_when_only_the_tag_changes():
    """Switching the backdrop to an artist photo changes the tag but not the
    cover's pixels: the cover must be re-sent, or the panel hides it."""
    link = FakeLink()
    images = {"art": ({"kind": "icon", "key": "art", "w": 1, "h": 1, "tag": "t1"}, b"same-cover")}
    up = ImageUploader(lambda: link, lambda: dict(images))
    up.start()
    try:
        up.offer(3, 0)
        assert wait_for(lambda: len(link.images) == 1)
        images["art"] = ({"kind": "icon", "key": "art", "w": 1, "h": 1, "tag": "t2"}, b"same-cover")
        up.offer(3, 1)
        assert wait_for(lambda: len(link.images) == 2)
    finally:
        up.stop()
