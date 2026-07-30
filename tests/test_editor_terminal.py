"""Tests for the focus-free editor-terminal path.

Two halves:
  1. The honest-failure guard in local_mac.set_text — an AX value-set into an editor's
     xterm.js terminal LOOKS like it succeeds (returns 0) but never reaches the PTY, so
     set_text must detect that case and refuse instead of reporting a phantom success.
  2. The CDP editor plumbing (cdp.py) — editor detection, dedicated port/profile, and the
     xterm-aware typing that routes a terminal 'type' through Input.insertText keystrokes
     rather than .value-setting (which xterm ignores).

Dependency-light: no live editor, no CDP socket — the AX and CDP calls are stubbed.
"""

import sys

import hunch.cdp as cdp
import hunch.local_mac as local_mac
from hunch import ax_tree_mac as ax


# ── 1. honest-failure guard ────────────────────────────────────────────────────
def _fake_attrs(role, title="", desc=""):
    return {ax.kAXRoleAttribute: role, ax.kAXTitleAttribute: title, ax.kAXDescriptionAttribute: desc}


def test_is_editor_terminal_detects_xterm(monkeypatch):
    monkeypatch.setattr(local_mac, "_embedded_chromium", lambda pid: "Electron Framework.framework")
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: _fake_attrs("AXTextArea", "Terminal 1, zsh"))
    assert local_mac._is_editor_terminal(object(), 123) is True


def test_is_editor_terminal_ignores_plain_electron_input(monkeypatch):
    # A normal Electron text box (Slack message, etc.) must NOT be misread as a terminal.
    monkeypatch.setattr(local_mac, "_embedded_chromium", lambda pid: "Electron Framework.framework")
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: _fake_attrs("AXTextArea", "Message #general"))
    assert local_mac._is_editor_terminal(object(), 123) is False


def test_is_editor_terminal_ignores_native_apps(monkeypatch):
    # Not embedded-Chromium -> never a terminal-guard case, even if named "Terminal".
    monkeypatch.setattr(local_mac, "_embedded_chromium", lambda pid: None)
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: _fake_attrs("AXTextArea", "Terminal 1, zsh"))
    assert local_mac._is_editor_terminal(object(), 123) is False


def test_is_editor_terminal_requires_text_role(monkeypatch):
    monkeypatch.setattr(local_mac, "_embedded_chromium", lambda pid: "Electron Framework.framework")
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: _fake_attrs("AXButton", "Terminal"))
    assert local_mac._is_editor_terminal(object(), 123) is False


def test_set_text_refuses_editor_terminal(monkeypatch):
    """set_text must NOT claim success on a terminal — it must return a refusal that points
    to the CDP path, and must never call AXUIElementSetAttributeValue."""
    sess = local_mac.MacSession()
    sess._app_name, sess._pid = "Cursor", 999
    monkeypatch.setattr(sess, "_el", lambda ref: object())
    monkeypatch.setattr(local_mac, "_is_editor_terminal", lambda el, pid: True)

    called = {"set": False}

    def _boom(*a, **k):
        called["set"] = True
        return 0
    monkeypatch.setattr(local_mac, "AXUIElementSetAttributeValue", _boom)

    msg = sess.set_text("e100", "claude agents\n")
    assert called["set"] is False, "must not attempt the phantom AX set"
    assert "terminal" in msg.lower() and "web_open" in msg
    assert "set text on" not in msg


# ── 2. CDP editor plumbing ─────────────────────────────────────────────────────
def test_editor_detection():
    for name in ("Cursor", "cursor", "code", "vscode", "VS Code", "windsurf", "vscodium"):
        assert cdp._is_editor(name), name
    for name in ("Google Chrome", "Arc", "Discord", "", "Safari"):
        assert not cdp._is_editor(name), name


def test_editor_target_is_dedicated_and_stable():
    port, profile, real = cdp.editor_target("cursor")
    assert real == "Cursor"
    assert "cursor-cdp" in profile
    assert 9360 <= port <= 9399         # off the browser port (9337)
    assert port != cdp.editor_target("code")[0] or real != "Visual Studio Code"
    # deterministic
    assert cdp.editor_target("cursor")[0] == port


def test_pick_workbench_skips_agents_side_panel():
    """An editor exposes the real window AND side panels sharing the same workbench.html url;
    they differ only by title. _pick_workbench must bind the editor, not 'Cursor Agents'."""
    wb = "vscode-file://vscode-app/.../electron-sandbox/workbench/workbench.html"
    pages = [
        {"id": "1", "title": "Cursor Agents", "url": wb},
        {"id": "2", "title": "my-project — Cursor", "url": wb},
    ]
    assert cdp._pick_workbench(pages)["id"] == "2"
    # order-independent
    assert cdp._pick_workbench(list(reversed(pages)))["id"] == "2"
    # only the side panel is up yet -> None (caller keeps polling for the real window)
    assert cdp._pick_workbench([pages[0]]) is None
    assert cdp._pick_workbench([]) is None


class _RecordingSession(cdp.CDPSession):
    """A CDPSession that records CDP commands instead of hitting a socket, and answers the
    xterm-detection probe as configured."""
    def __init__(self, is_terminal):
        self.calls = []
        self._is_terminal = is_terminal
        self.registry = {"e5": 42}

    def _cmd(self, method, params=None, timeout=20):
        self.calls.append((method, params or {}))
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-1"}}
        if method == "Runtime.callFunctionOn":
            fn = (params or {}).get("functionDeclaration", "")
            if "xterm-helper-textarea" in fn:      # the _XTERM_FOCUS_FN probe
                return {"result": {"value": "TERMINAL" if self._is_terminal else "NOT_TERMINAL"}}
            return {"result": {"value": "set"}}     # the _SET_FN path
        return {}


def test_terminal_type_uses_keystrokes_not_value_set():
    """Typing into a terminal ref must go through Input.insertText + an Enter keystroke for the
    trailing newline, and must NOT set the textarea .value (which xterm ignores)."""
    s = _RecordingSession(is_terminal=True)
    out = s.type_text("e5", "claude agents\n")
    methods = [m for m, _ in s.calls]
    assert "Input.insertText" in methods
    inserted = [p["text"] for m, p in s.calls if m == "Input.insertText"]
    assert inserted == ["claude agents"]            # newline stripped, sent as Enter
    enters = [p for m, p in s.calls if m == "Input.dispatchKeyEvent" and p.get("key") == "Enter"]
    assert enters, "trailing newline must become an Enter keystroke (run the command)"
    assert "ran it" in out
    # only the xterm probe should have run — never the .value-setting _SET_FN
    fns = [p.get("functionDeclaration", "") for m, p in s.calls if m == "Runtime.callFunctionOn"]
    assert len(fns) == 1 and "xterm-helper-textarea" in fns[0]


def test_nonterminal_type_still_sets_value():
    """A normal field must still use the .value-setting path, not the terminal keystroke path."""
    s = _RecordingSession(is_terminal=False)
    out = s.type_text("e5", "hello")
    fns = [p.get("functionDeclaration", "") for m, p in s.calls if m == "Runtime.callFunctionOn"]
    assert any("HTMLTextAreaElement" in f or "FALLBACK" in f for f in fns), "should reach _SET_FN"
    assert "e5: set" in out


def test_insert_stream_splits_newlines():
    s = _RecordingSession(is_terminal=True)
    s.calls = []
    s._insert_stream("line1\nline2\n")
    inserts = [p["text"] for m, p in s.calls if m == "Input.insertText"]
    enter_downs = [1 for m, p in s.calls
                   if m == "Input.dispatchKeyEvent" and p.get("key") == "Enter" and p["type"] == "keyDown"]
    assert inserts == ["line1", "line2"]
    assert len(enter_downs) == 2         # one Enter per newline (each = a keyDown+keyUp pair)


def test_backtick_key_has_backquote_code():
    """ctrl+` (open terminal) needs DOM code 'Backquote' or the editor keybinding won't fire."""
    s = _RecordingSession(is_terminal=True)
    s.calls = []
    s.press_key("`", ["ctrl"])
    downs = [p for m, p in s.calls if m == "Input.dispatchKeyEvent"]
    assert downs and all(p["code"] == "Backquote" for p in downs)
    assert all(p["modifiers"] == 2 for p in downs)   # ctrl bit


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
