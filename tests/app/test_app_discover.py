"""LinkManager: USB first, Wi-Fi fallback, token handover, reconnects."""
from pandadeck.link.base import LinkError
from pandadeck.link.discover import FAILS_BEFORE_DROP, LinkManager


class FakeLink:
    def __init__(self, kind, where, info=None, fail_hello=False):
        self.kind, self.where = kind, where
        self.info = info or {"app": "pandadeck", "fw": "1.0.0"}
        self.fail_hello, self.closed, self.dead = fail_hello, False, False

    def hello(self):
        if self.fail_hello:
            raise LinkError("no answer")
        return dict(self.info)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(settings, secrets, state, usb_ports=(), usb_links=None, wifi=None, resolve=lambda: None, clock=None):
    usb_links = usb_links or {}
    made = []

    def open_usb(port):
        if port not in usb_links:
            raise OSError("busy")
        made.append(port)
        return usb_links[port]

    def make_wifi(host, token, require_proof=False):
        if wifi is None:
            raise LinkError("unreachable")
        wifi.where, wifi.token, wifi.require_proof = host, token, require_proof
        return wifi

    m = LinkManager(settings, secrets, state, ports=lambda preferred_serial="": list(usb_ports), open_usb=open_usb,
                    make_wifi=make_wifi, resolve=resolve, serial_of=lambda p: "SER-" + p, clock=clock or Clock())
    return m, made


def test_usb_wins_and_hands_over_the_token(settings, fake_secrets, fake_state):
    usb = FakeLink("usb", "COM5", {"app": "pandadeck", "fw": "1.0.0", "token": "T0K", "wifi": {"ip": "10.0.0.7"}})
    connected = []
    m, _ = make(settings, fake_secrets, fake_state, ["COM5"], {"COM5": usb}, wifi=FakeLink("wifi", ""))
    m.on_connect.append(lambda link, info: connected.append((link.kind, info)))
    m.tick()
    st = m.status()
    assert st["state"] == "connected" and st["kind"] == "usb" and st["where"] == "COM5"
    assert fake_secrets["panel_token"] == "T0K" and "token" not in st["info"]
    assert "token" not in connected[0][1] and fake_state["last_ip"] == "10.0.0.7"
    assert fake_state["usb_serial"] == "SER-COM5"


def test_non_pandadeck_ports_are_closed_and_skipped(settings, fake_secrets, fake_state):
    other = FakeLink("usb", "COM8", {"app": "something-else"})
    silent = FakeLink("usb", "COM9", fail_hello=True)
    m, _ = make(settings, fake_secrets, fake_state, ["COM7", "COM8", "COM9"], {"COM8": other, "COM9": silent})
    m.tick()
    assert m.current() is None and other.closed and silent.closed
    assert "COM7" in m.error  # busy port reported


def test_wifi_fallback_needs_token_and_uses_last_ip(settings, fake_secrets, fake_state):
    wifi = FakeLink("wifi", "")
    m, _ = make(settings, fake_secrets, fake_state, [], wifi=wifi)
    fake_state.set("last_ip", "10.0.0.7")
    m.tick()
    assert m.current() is None and "USB" in m.error  # no token yet: explains how to pair
    fake_secrets.set("panel_token", "T0K")
    m._next_wifi = 0
    m.tick()
    assert m.current() is wifi and wifi.where == "10.0.0.7" and wifi.token == "T0K"
    assert wifi.require_proof  # an automatic address must prove it's the panel before getting the token


def test_wifi_uses_configured_host_then_mdns(fake_secrets, fake_state, make_settings):
    FakeSettings = make_settings  # noqa: N806
    s = FakeSettings({"panel": {"link": "wifi", "host": "panel.lan"}})
    fake_secrets.set("panel_token", "T")
    wifi = FakeLink("wifi", "")
    m, made = make(s, fake_secrets, fake_state, ["COM5"], {"COM5": FakeLink("usb", "COM5")}, wifi=wifi)
    m.tick()
    assert m.current() is wifi and wifi.where == "panel.lan" and made == []  # wifi-only never opens USB
    assert wifi.require_proof is False  # a typed address is the user's call
    s2 = FakeSettings({"panel": {"link": "wifi"}})
    wifi2 = FakeLink("wifi", "")
    m2, _ = make(s2, fake_secrets, FakeStateEmpty(), [], wifi=wifi2, resolve=lambda: "10.0.0.88")
    m2.tick()
    assert wifi2.where == "10.0.0.88" and wifi2.require_proof


class FakeStateEmpty(dict):
    def get(self, k, d=None):  # noqa: D401
        return d

    def set(self, k, v):
        pass


def test_wifi_retry_is_rate_limited(settings, fake_secrets, fake_state):
    clock = Clock()
    fake_secrets.set("panel_token", "T")
    fake_state.set("last_ip", "10.0.0.7")
    m, _ = make(settings, fake_secrets, fake_state, [], wifi=None, clock=clock)
    m.tick()
    first_next = m._next_wifi
    m.tick()
    assert m._next_wifi == first_next  # no second attempt yet
    clock.t += 11
    m.tick()
    assert m._next_wifi > first_next


def test_switches_from_wifi_to_usb_when_plugged_in(settings, fake_secrets, fake_state):
    clock = Clock()
    fake_secrets.set("panel_token", "T")
    fake_state.set("last_ip", "10.0.0.7")
    wifi = FakeLink("wifi", "")
    ports = []
    usb = FakeLink("usb", "COM5")
    m, _ = make(settings, fake_secrets, fake_state, ports, {"COM5": usb}, wifi=wifi, clock=clock)
    m._ports = lambda preferred_serial="": list(ports)
    m.tick()
    assert m.current() is wifi
    ports.append("COM5")
    clock.t += 11
    m.tick()
    assert m.current() is usb and wifi.closed


def test_failures_drop_the_link_after_a_few(settings, fake_secrets, fake_state):
    usb = FakeLink("usb", "COM5")
    m, _ = make(settings, fake_secrets, fake_state, ["COM5"], {"COM5": usb})
    m.tick()
    for _ in range(FAILS_BEFORE_DROP - 1):
        m.failed(LinkError("busy"))
    assert m.current() is usb
    m.succeeded()
    for _ in range(FAILS_BEFORE_DROP - 1):
        m.failed(LinkError("busy"))
    assert m.current() is usb  # counter was reset by the success
    m.failed(LinkError("busy"))
    assert m.current() is None and usb.closed


def test_dead_usb_drops_at_once(settings, fake_secrets, fake_state):
    usb = FakeLink("usb", "COM5")
    m, _ = make(settings, fake_secrets, fake_state, ["COM5"], {"COM5": usb})
    m.tick()
    usb.dead = True
    m.failed(LinkError("gone"))
    assert m.current() is None


def test_disabled_and_busy_managers_do_nothing(settings, fake_secrets, fake_state):
    m, made = make(settings, fake_secrets, fake_state, ["COM5"], {"COM5": FakeLink("usb", "COM5")})
    m.enabled = False
    m.tick()
    assert made == [] and m.status()["state"] == "off"
    m.enabled, m.busy = True, "update"
    m.tick()
    assert made == []


def test_reconnect_and_demo(settings, fake_secrets, fake_state):
    usb = FakeLink("usb", "COM5")
    m, _ = make(settings, fake_secrets, fake_state, ["COM5"], {"COM5": usb})
    m.tick()
    m.reconnect()
    assert m.current() is None and usb.closed
    d, _ = make(settings, fake_secrets, fake_state)
    d.demo = True
    d.tick()
    assert d.current().where == "demo" and d.status()["info"]["demo"]


def test_silent_and_foreign_ports_back_off(settings, fake_secrets, fake_state):
    clock = Clock()
    other = FakeLink("usb", "COM8", {"app": "something-else"})
    silent = FakeLink("usb", "COM9", fail_hello=True)
    m, made = make(settings, fake_secrets, fake_state, ["COM8", "COM9"], {"COM8": other, "COM9": silent}, clock=clock)
    m.tick()
    assert made == ["COM8", "COM9"]
    m.tick()
    assert made == ["COM8", "COM9"]  # neither re-probed right away
    clock.t += 11
    m.tick()
    assert made == ["COM8", "COM9", "COM9"]  # the silent one (maybe a booting panel) comes back first


# ---- "What works right now": how the panel is connected ------------------------------------------

def test_link_mode():
    from pandadeck.link.discover import link_mode
    assert link_mode("connected", "usb", {"usb": True}) == "usb"           # cable to this computer
    assert link_mode("connected", "wifi", {"usb": False}) == "wifi"        # charger only
    assert link_mode("connected", "wifi", {}) == "wifi"                    # older firmware: no usb flag
    assert link_mode("connected", "wifi", {"usb": True}) == "usb_other"    # keyboard on another computer
    assert link_mode("searching", "", {}) == "none" and link_mode("off", "", None) == "none"


def test_panel_info_is_refreshed_while_connected(settings, fake_secrets, fake_state):
    from pandadeck.link.discover import INFO_REFRESH_S
    settings.update({"panel": {"link": "wifi", "host": "10.0.0.7"}})
    fake_secrets.set("panel_token", "T")
    clock = Clock()
    wifi = FakeLink("wifi", "", {"app": "pandadeck", "fw": "0.6", "usb": False})
    m, _ = make(settings, fake_secrets, fake_state, wifi=wifi, clock=clock)
    m.tick()
    assert m.status()["mode"] == "wifi"
    wifi.info = {"app": "pandadeck", "fw": "0.6", "usb": True, "token": "never-shown"}  # cable into another PC
    clock.t += 5
    m.tick()
    assert m.status()["mode"] == "wifi"                                    # not re-read yet
    clock.t += INFO_REFRESH_S
    m.tick()
    st = m.status()
    assert st["mode"] == "usb_other" and "token" not in st["info"]
    wifi.fail_hello = True
    clock.t += INFO_REFRESH_S
    m.tick()
    assert m.status()["mode"] == "usb_other" and m.status()["state"] == "connected"  # a failed re-read changes nothing
