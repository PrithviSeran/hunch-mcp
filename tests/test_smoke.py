"""Dependency-light smoke tests: imports, policy defaults, and the two guards.

Run: .venv/bin/python -m pytest tests/  (or plain `python tests/test_smoke.py`).
Needs pyobjc (imports the server) but touches no Keychain items and no UI.
"""

import os
import sys

import hunch
import hunch.creds as creds
import hunch.policy as policy
import hunch.server as server


def test_version():
    assert hunch.__version__


def test_tool_count():
    assert len(server.mcp._tool_manager._tools) == 29
    assert "find" in server.mcp._tool_manager._tools


def test_policy_defaults_all_on():
    for cat in policy.DEFAULT_GATES:
        assert policy.gate_enabled(cat) or _suppressed(), cat


def _suppressed():
    return bool(os.environ.get("HUNCH_NO_INTERNAL_GATE")) or policy.load().get("auto_approve_all")


def test_env_override_wins(monkeypatch=None):
    old = os.environ.get("HUNCH_NO_INTERNAL_GATE")
    os.environ["HUNCH_NO_INTERNAL_GATE"] = "1"
    try:
        assert not policy.gate_enabled("shell")
    finally:
        if old is None:
            del os.environ["HUNCH_NO_INTERNAL_GATE"]
        else:
            os.environ["HUNCH_NO_INTERNAL_GATE"] = old


def test_protected_paths():
    assert server._protected("~/.hunch")
    assert server._protected("~/.hunch/config.json")
    assert server._protected(os.path.expanduser("~/.hunch/creds_index.json"))
    assert not server._protected("~/Desktop/file.txt")
    assert not server._protected("~/.hunchbackup")  # prefix, not inside


def test_norm_domain():
    assert creds._norm_domain("https://www.Accounts.Google.com:443/x") == "accounts.google.com"
    assert creds._norm_domain("GOOGLE.com") == "google.com"
    assert creds._norm_domain("") == ""


def test_app_to_front_gate_default_on():
    assert "app_to_front" in policy.DEFAULT_GATES
    assert "gates.app_to_front" in policy.CONFIG_KEYS


def test_screen_approval_dedupe():
    import hunch.local_mac as local_mac
    # A fresh approval lets the front gate pass with NO dialog...
    server._gate.mark_screen_approval()
    assert server._gate.screen_approved()
    assert server._gate.front_gate("SomeApp", "test") is None
    # ...and suppresses the focus notification at the choke point.
    fired = []
    old_notify, old_until = local_mac._notify_focus, local_mac._suppress_until
    try:
        local_mac._notify_focus = lambda name, reason="": fired.append(name)
        local_mac._announce_front("DefinitelyNotFrontmostApp")
        assert fired == [], "notification should be suppressed right after approval"
        local_mac._suppress_until = 0.0
        local_mac._announce_front("DefinitelyNotFrontmostApp")
        assert fired == ["DefinitelyNotFrontmostApp"], "notification should fire when not suppressed"
    finally:
        local_mac._notify_focus, local_mac._suppress_until = old_notify, old_until
        server._gate._screen_ok_until = 0.0


class _StubSession:
    def __init__(self, url):
        self._url = url

    def url(self):
        return self._url


class _StubComputer:
    def __init__(self, url):
        self.session = _StubSession(url)


def test_domain_mismatch_guard():
    """The domain guard, wired through the INVERTED server: web_fill_login goes
    server tool -> _dispatch_core -> _mac.web.fill_login -> gate.domain_mismatch
    with the live CDP page URL."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        old_meta = creds._meta_path
        creds._meta_path = lambda base, ns=None: os.path.join(d, ns or "personal", base)
        old_comp = server._mac.web._computer
        try:
            creds._write_index(["testsvc"])   # has() runs before the domain guard
            creds.set_domains("testsvc", ["google.com"])
            server._mac.web._computer = _StubComputer("https://evil.example.com/login")
            refusal = server._run("web_fill_login", service="testsvc")
            assert refusal and refusal.startswith("REFUSED")
            # allowed subdomain + unbound service pass the guard (they fail later at
            # the has()/keychain stage in this stub setup, but must NOT be REFUSED)
            server._mac.web._computer = _StubComputer("https://accounts.google.com/signin")
            assert not server._run("web_fill_login", service="unbound-svc").startswith("REFUSED")
        finally:
            creds._meta_path = old_meta
            server._mac.web._computer = old_comp


if __name__ == "__main__":
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        getattr(mod, name)()
        print(f"ok {name}")
    print("all smoke tests passed")
