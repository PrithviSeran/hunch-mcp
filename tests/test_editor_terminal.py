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


WB = "vscode-file://vscode-app/.../electron-sandbox/workbench/workbench.html"


def _win(i, title):
    return {"id": str(i), "title": title, "url": WB}


def test_pick_workbench_prefers_the_requested_folder():
    """The regression that cost a whole session: an editor instance holds several windows (it
    restores the last session's on launch), so 'the first workbench' is a coin flip. Asked for a
    folder, _pick_workbench must return the window actually holding it — not window order."""
    pages = [_win(1, "a3_q1_solution.html — C63"), _win(2, "hunch"), _win(3, "notes — hunch-mcp")]
    assert cdp._pick_workbench(pages, "/Users/me/hunch")["id"] == "2"
    # a similar-but-different folder name must NOT match ('hunch-mcp' is not 'hunch')
    assert cdp._pick_workbench(pages, "/Users/me/hunch-mcp")["id"] == "3"
    # trailing slash, and a folder whose window is open with a file focused
    assert cdp._pick_workbench(pages, "/Users/me/hunch/")["id"] == "2"
    assert cdp._pick_workbench(pages, "/Users/me/C63")["id"] == "1"
    # no window has it -> falls back to a real workbench, and the CALLER must not claim success
    assert cdp._pick_workbench(pages, "/Users/me/absent")["id"] == "1"
    # no folder asked for -> unchanged first-workbench behaviour
    assert cdp._pick_workbench(pages)["id"] == "1"


def test_title_matching_is_dash_delimited_not_substring():
    assert cdp._title_matches("a3.html — C63", "/x/C63") is True
    assert cdp._title_matches("hunch", "/x/hunch") is True
    # a hyphenated folder name must not be split into pieces
    assert cdp._title_matches("my-project — Cursor", "/x/my-project") is True
    assert cdp._title_matches("my-project — Cursor", "/x/my") is False
    # substring of a longer workspace name is not a match
    assert cdp._title_matches("hunch-mcp", "/x/hunch") is False
    assert cdp._title_matches("", "/x/hunch") is False
    assert cdp._title_matches("hunch", "") is False


def test_title_workspace():
    assert cdp._title_workspace("a3_q1_solution.html — C63") == "C63"
    assert cdp._title_workspace("hunch") == "hunch"
    assert cdp._title_workspace("") == ""


class _FakeTargets(cdp.CDPSession):
    """A session whose target list and websocket binding are stubbed."""

    def __init__(self, pages):
        super().__init__(port=0)
        self._pages = pages
        self.bound = []

    def _list_page_targets(self):
        return self._pages

    def _open_ws(self, page):
        self.target_id = page.get("id")
        self.bound.append(page.get("id"))


def test_bind_workspace_binds_the_matching_window_and_pins_it():
    s = _FakeTargets([_win(1, "a3.html — C63"), _win(2, "README.md — hunch")])
    s.target_id = "1"
    assert s.bind_workspace("/Users/me/hunch", timeout=0) is True
    assert s.target_id == "2"
    assert s.pinned == "/Users/me/hunch"


def test_bind_workspace_reports_failure_instead_of_binding_something_else():
    """The window isn't there. Returning True on the nearest window is what produced a
    confident 'opened Cursor on ~/hunch' while the session drove an unrelated project."""
    s = _FakeTargets([_win(1, "a3.html — C63")])
    s.target_id = "1"
    assert s.bind_workspace("/Users/me/hunch", timeout=0) is False
    assert s.pinned is None
    assert s.bound == []


def test_editor_session_does_not_auto_follow_new_windows():
    """Browsers should follow a newly opened tab; an editor must NOT — a new target there is
    another window or a side panel, and following it silently moves the agent off its workspace."""
    s = _FakeTargets([_win(1, "README.md — hunch")])
    s.editor = True
    s.target_id = "1"
    s._known_targets = {"1"}
    s._pages = [_win(1, "README.md — hunch"), _win(2, "Cursor Agents")]
    assert s._follow_new_tab() is False
    assert s.target_id == "1"


def test_editor_session_rebinds_when_its_own_window_closes():
    s = _FakeTargets([_win(1, "README.md — hunch")])
    s.editor = True
    s.pinned = "/Users/me/hunch"
    s.target_id = "9"          # our window is gone
    s._known_targets = {"9"}
    assert s._follow_new_tab() is True
    assert s.target_id == "1"


# ── web.open(editor) must verify the workspace, never assert it ────────────────
class _StubEditorSession:
    def __init__(self, workspace, opens_on_request=None):
        self._ws = workspace
        self._opens_on_request = opens_on_request   # workspace it switches to when asked
        self.pinned = None
        self.editor = True
        self.binds = 0

    def wait_ready(self):
        return True

    def workspace(self):
        return self._ws

    def windows(self):
        return [{"index": 0, "title": self._ws, "workspace": self._ws, "current": True}]

    def bind_workspace(self, folder, timeout=12):
        self.binds += 1
        if cdp._title_matches(self._ws, folder):
            self.pinned = folder
            return True
        return False


class _StubEditorComputer:
    def __init__(self, session):
        self.session = session

    def snapshot(self):
        return "[e1] window\n[e2] .xterm terminal"


def _web_with(session, monkeypatch):
    """A real Web object running the real _open_editor, with only the CDP launch stubbed."""
    from hunch import sdk
    web = object.__new__(sdk.Web)
    web.close = lambda: None
    monkeypatch.setattr(cdp, "CDPComputer", lambda *a, **k: _StubEditorComputer(session))
    return web


def test_open_editor_refuses_to_claim_a_workspace_it_never_reached(monkeypatch, tmp_path):
    """The exact regression: the instance is live on someone else's project, the requested folder
    is dropped, and the old code answered 'opened Cursor on /Users/me/hunch'."""
    folder = tmp_path / "hunch"
    folder.mkdir()
    session = _StubEditorSession("C63")               # never switches
    web = _web_with(session, monkeypatch)
    monkeypatch.setattr(cdp, "open_folder_in_editor", lambda *a, **k: True)
    out = web._open_editor(folder=str(folder), app="Cursor")
    assert "could NOT open" in out and "C63" in out
    assert "opened Cursor" not in out
    assert session.binds == 2                          # tried, handed the folder over, retried


def test_open_editor_reports_success_only_once_bound(monkeypatch, tmp_path):
    folder = tmp_path / "hunch"
    folder.mkdir()
    session = _StubEditorSession("hunch")
    web = _web_with(session, monkeypatch)
    out = web._open_editor(folder=str(folder), app="Cursor")
    assert out.startswith("opened Cursor on ")
    assert session.pinned == str(folder)
    assert session.binds == 1                          # matched first try, no extra window


def test_open_editor_rejects_a_missing_path_before_launching(monkeypatch, tmp_path):
    session = _StubEditorSession("C63")
    web = _web_with(session, monkeypatch)
    out = web._open_editor(folder=str(tmp_path / "nope"), app="Cursor")
    assert "no such path" in out
    assert session.binds == 0                          # nothing launched, nothing polled


class _RecordingSession(cdp.CDPSession):
    """A CDPSession that records CDP commands instead of hitting a socket, and answers the
    xterm-detection probe as configured."""
    def __init__(self, is_terminal, select_kind="OK"):
        self.calls = []
        self._is_terminal = is_terminal
        self._select_kind = select_kind
        self.registry = {"e5": 42}

    def _cmd(self, method, params=None, timeout=20):
        self.calls.append((method, params or {}))
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-1"}}
        if method == "Runtime.callFunctionOn":
            fn = (params or {}).get("functionDeclaration", "")
            if "xterm-helper-textarea" in fn:      # the _XTERM_FOCUS_FN probe
                return {"result": {"value": "TERMINAL" if self._is_terminal else "NOT_TERMINAL"}}
            if "IS_SELECT" in fn:                  # the _SELECT_KIND_FN probe
                return {"result": {"value": self._select_kind}}
            return {"result": {"value": "set"}}     # the _SET_FN path
        if method == "DOM.getBoxModel":
            return {"model": {"content": [0, 0, 10, 0, 10, 10, 0, 10]}}
        return {}


def test_click_refuses_native_select():
    s = _RecordingSession(is_terminal=False, select_kind="IS_SELECT")
    out = s.click("e5")
    assert out.startswith("REFUSED") and "VISIBLE TEXT" in out
    assert not any(m == "Input.dispatchMouseEvent" for m, _ in s.calls)


def test_click_refuses_select_option_node():
    s = _RecordingSession(is_terminal=False, select_kind="IS_OPTION")
    out = s.click("e5")
    assert out.startswith("REFUSED") and "option" in out.lower()


def test_click_still_dispatches_for_normal_elements():
    s = _RecordingSession(is_terminal=False, select_kind="OK")
    out = s.click("e5")
    assert out == "clicked e5"
    assert any(m == "Input.dispatchMouseEvent" for m, _ in s.calls)


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
