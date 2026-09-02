"""Dependency-light smoke tests: imports, policy defaults, and the two guards.

Run: .venv/bin/python -m pytest tests/  (or plain `python tests/test_smoke.py`).
Needs pyobjc (imports the server) but touches no Keychain items and no UI.
"""

import os
import sys
import tomllib
from pathlib import Path

import hunch
import hunch.creds as creds
import hunch.policy as policy
import hunch.server as server


def test_version():
    assert hunch.__version__


def test_runtime_version_matches_package_metadata():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project_version = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert hunch.__version__ == project_version


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


def test_user_attention_notifications_default_off(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "CONFIG_PATH", str(tmp_path / "config.json"))
    assert "notifications.user_attention" in policy.CONFIG_KEYS
    assert policy.user_attention_notifications_enabled() is False
    cfg = policy.default_config()
    policy.set_key(cfg, "notifications.user_attention", True)
    policy.save(cfg)
    assert policy.user_attention_notifications_enabled() is True


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


# ── wrong-target guards ────────────────────────────────────────────────────────
def test_applescript_empty_result_explains_electron_zero_windows():
    """`count of windows` via System Events returns 0 for any Electron app, however many are
    open. Without a reason the natural move is to retry the same script, which can never work."""
    from hunch import gate
    hint = gate.applescript_empty_hint(
        'tell application "System Events" to tell process "Cursor" to count of windows')
    assert "Electron" in hint and "snapshot(app=" in hint
    # unrelated scripts get no noise
    assert gate.applescript_empty_hint('tell application "Music" to play') == ""
    assert gate.applescript_empty_hint('tell application "System Events" to keystroke "a"') == ""


def test_applescript_settings_refusal_blocks_defaults_and_ui_script():
    """Freeze miss mode: agents invent defaults write / System Events clicks for Settings."""
    from hunch import gate
    msg = gate.applescript_settings_refusal(
        'do shell script "defaults write com.apple.dock launchanim -bool true"')
    assert msg and msg.startswith("REFUSED") and "AXCheckBox" in msg
    msg2 = gate.applescript_settings_refusal(
        'tell application "System Events" to tell process "System Settings" to click checkbox 1')
    assert msg2 and msg2.startswith("REFUSED") and "snapshot" in msg2
    assert gate.applescript_settings_refusal('tell application "Music" to play') is None


def test_playbook_covers_toggle_and_select_policy():
    from hunch.playbook import HUNCH_PLAYBOOK
    pb = HUNCH_PLAYBOOK
    assert "val='0'" in pb and "val='1'" in pb
    assert "defaults write" in pb
    assert "VISIBLE TEXT" in pb


def test_twin_process_warning_names_which_copy_the_tree_is(monkeypatch):
    """Hunch's CDP-driven Cursor and the user's own are two processes with one name: the AX
    tools can read one while web_* drives the other, both looking perfectly valid."""
    from hunch import local_mac as lm
    lm._twin_cache.clear()
    monkeypatch.setattr(lm, "_pids_named", lambda n: [111, 222])
    monkeypatch.setattr(lm, "_proc_cmdline",
                        lambda pid: "Cursor --user-data-dir=/Users/x/.hunch/cursor-cdp"
                        if pid == 222 else "Cursor")
    warn = lm._twin_process_warning("Cursor", 111)
    assert "USER's copy" in warn and "222" in warn and "web_snapshot" in warn

    lm._twin_cache.clear()
    warn_hunch = lm._twin_process_warning("Cursor", 222)
    assert "HUNCH's CDP-driven copy" in warn_hunch

    # single process -> silent
    lm._twin_cache.clear()
    monkeypatch.setattr(lm, "_pids_named", lambda n: [111])
    assert lm._twin_process_warning("Cursor", 111) == ""


if __name__ == "__main__":
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        getattr(mod, name)()
        print(f"ok {name}")
    print("all smoke tests passed")
