"""Unit tests for the AX-exhaustive click path (no UI, all AX calls faked):
_ax_fire's press → alternative-action → value-flip ladder, _ax_activate's
descendant sweep, and the pixel fallback honoring the activate-or-refuse
contract instead of moving the shared cursor blind.

Run: .venv/bin/python -m pytest tests/test_ax_press.py
"""

import hunch.local_mac as lm
from hunch import ax_tree_mac as ax


def _bare_session():
    s = object.__new__(lm.MacSession)
    s._app_name = "FakeApp"
    s.disturbances = {"pixel_clicks": 0, "keystrokes": 0, "key_combos": 0, "app_raises": 0}
    return s


def test_ax_fire_flips_switch_value_with_readback(monkeypatch):
    """Checkbox whose AXPress fails: _ax_fire flips AXValue and only reports
    success after reading the new value back."""
    state = {"v": 0}
    monkeypatch.setattr(lm, "AXUIElementPerformAction", lambda el, a: -25200)
    monkeypatch.setattr(ax, "get_actions", lambda el: [])
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: {
        ax.kAXRoleAttribute: "AXCheckBox", "AXSubrole": "AXSwitch",
        lm.kAXValueAttribute: state["v"]})
    monkeypatch.setattr(ax, "get_attr", lambda el, attr: state["v"])
    monkeypatch.setattr(lm, "AXUIElementSetAttributeValue",
                        lambda el, attr, v: state.update(v=v) or 0)
    monkeypatch.setattr(lm.time, "sleep", lambda s: None)
    assert _bare_session()._ax_fire("el") == "toggled to 1"
    assert state["v"] == 1


def test_ax_fire_rejects_dropped_value_write(monkeypatch):
    """SwiftUI switches accept the AXValue write and silently drop it — the
    read-back must catch that and report failure."""
    monkeypatch.setattr(lm, "AXUIElementPerformAction", lambda el, a: -25200)
    monkeypatch.setattr(ax, "get_actions", lambda el: [])
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: {
        ax.kAXRoleAttribute: "AXCheckBox", "AXSubrole": "AXSwitch",
        lm.kAXValueAttribute: 0})
    monkeypatch.setattr(ax, "get_attr", lambda el, attr: 0)  # write dropped
    monkeypatch.setattr(lm, "AXUIElementSetAttributeValue", lambda el, attr, v: 0)
    monkeypatch.setattr(lm.time, "sleep", lambda s: None)
    assert _bare_session()._ax_fire("el") is None


def test_ax_fire_prefers_alternative_action_over_toggle(monkeypatch):
    monkeypatch.setattr(lm, "AXUIElementPerformAction",
                        lambda el, a: 0 if a == "AXOpen" else -25200)
    monkeypatch.setattr(ax, "get_actions", lambda el: ["AXOpen", "AXScrollToVisible"])
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: {
        ax.kAXRoleAttribute: "AXButton", "AXSubrole": "", lm.kAXValueAttribute: None})
    msg = _bare_session()._ax_fire("el")
    assert msg == "performed AXOpen"


def test_static_text_press_success_is_not_trusted(monkeypatch):
    """macOS returns AXPress success on labels without doing anything — _ax_fire
    must not report that as activation."""
    monkeypatch.setattr(lm, "AXUIElementPerformAction", lambda el, a: 0)
    monkeypatch.setattr(ax, "get_actions", lambda el: [])
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: {
        ax.kAXRoleAttribute: "AXStaticText", "AXSubrole": "", lm.kAXValueAttribute: None})
    assert _bare_session()._ax_fire("label") is None


def test_label_ref_finds_sibling_switch_via_parent(monkeypatch):
    """Ref is the row's LABEL; the switch is a sibling — parent sweep must find it."""
    roles = {"label": "AXStaticText", "row": "AXGroup", "switch": "AXCheckBox"}
    pressed = []
    monkeypatch.setattr(lm, "AXUIElementPerformAction",
                        lambda el, a: (pressed.append((el, a)) or 0)
                        if el == "switch" and a == "AXPress" else -25200)
    monkeypatch.setattr(ax, "get_actions", lambda el: [])
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: {
        ax.kAXRoleAttribute: roles.get(el, "AXGroup"),
        "AXSubrole": "AXSwitch" if el == "switch" else "",
        lm.kAXValueAttribute: 1 if el == "switch" else None})
    monkeypatch.setattr(ax, "get_attr", lambda el, attr:
                        "row" if (el, attr) == ("label", "AXParent")
                        else (["label", "switch"] if (el, attr) == ("row", "AXChildren")
                              else ([] if attr == "AXChildren" else None)))
    msg = _bare_session()._ax_activate("label")
    assert msg == "pressed (inner control)"
    assert pressed == [("switch", "AXPress")]


def test_ax_activate_finds_inner_control(monkeypatch):
    """Ref lands on a wrapper row; the real pressable control is a child."""
    monkeypatch.setattr(lm, "AXUIElementPerformAction",
                        lambda el, a: 0 if el == "switch" else -25200)
    monkeypatch.setattr(ax, "get_actions", lambda el: [])
    monkeypatch.setattr(ax, "get_attrs", lambda el, attrs: {
        ax.kAXRoleAttribute: "AXGroup", "AXSubrole": "", lm.kAXValueAttribute: None})
    monkeypatch.setattr(ax, "get_attr", lambda el, attr:
                        ["switch"] if el == "row" else [])
    msg = _bare_session()._ax_activate("row")
    assert msg == "pressed (inner control)"


def test_click_pixel_fallback_refuses_without_activation(monkeypatch):
    """No AX vocabulary anywhere and the app can't be raised: the click must be
    refused — never post cursor events blind."""
    clicks = []
    s = _bare_session()
    s.registry = {"e1": "el"}
    monkeypatch.setattr(lm.MacSession, "_ax_activate", lambda self, el: None)
    monkeypatch.setattr(lm.MacSession, "_center", lambda self, el: (10, 20))
    monkeypatch.setattr(lm.MacSession, "activate", lambda self: False)
    monkeypatch.setattr(lm, "_mouse_click", lambda *a, **k: clicks.append(a))
    out = s.click("e1")
    assert "skipped" in out
    assert clicks == []


def test_click_pixel_fallback_announces_cursor_use(monkeypatch):
    clicks = []
    s = _bare_session()
    s.registry = {"e1": "el"}
    monkeypatch.setattr(lm.MacSession, "_ax_activate", lambda self, el: None)
    monkeypatch.setattr(lm.MacSession, "_center", lambda self, el: (10, 20))
    monkeypatch.setattr(lm.MacSession, "activate", lambda self: True)
    monkeypatch.setattr(lm, "_mouse_click", lambda *a, **k: clicks.append(a))
    out = s.click("e1")
    assert clicks == [(10, 20)]
    assert "moved the shared cursor" in out
    assert s.disturbances["pixel_clicks"] == 1  # the receipt counter saw it


def test_file_op_batch_runs_all_and_reports_per_item():
    import hunch.agent as ag

    class FakeFiles:
        def __init__(self): self.calls = []
        def move(self, s, d): self.calls.append(("move", s, d)); return f"moved {s} -> {d}"
        def copy(self, s, d): self.calls.append(("copy", s, d)); return f"copied {s} -> {d}"
        def mkdir(self, s): self.calls.append(("mkdir", s, "")); return f"created {s}"

    class FakeMac:
        files = FakeFiles()

    mac = FakeMac()
    out = ag._file_op(mac, {"batch": [
        {"op": "mkdir", "src": "/tmp/x"},
        {"op": "move", "src": "/tmp/a", "dst": "/tmp/x"},
        {"op": "bogus", "src": "/tmp/b"},
        {"op": "copy", "src": "/tmp/c", "dst": "/tmp/x"},
    ]})
    lines = out.splitlines()
    assert len(lines) == 4
    assert lines[0] == "created /tmp/x"
    assert "unknown op 'bogus'" in lines[2]
    assert mac.files.calls == [("mkdir", "/tmp/x", ""), ("move", "/tmp/a", "/tmp/x"),
                               ("copy", "/tmp/c", "/tmp/x")]
    # single-op path unchanged
    assert ag._file_op(mac, {"op": "move", "src": "/a", "dst": "/b"}) == "moved /a -> /b"


PREV = """=== App — focused window (snapshot #3) ===
[e1] AXWindow "Motion"
  [e2] AXStaticText val='Reduce motion'
  [e3] AXCheckBox val=0
  [e4] AXButton 'Done'"""

def test_snapshot_delta_reports_only_changes():
    cur = PREV.replace("snapshot #3", "snapshot #4").replace("[e3] AXCheckBox val=0",
                                                             "[e3] AXCheckBox val=1")
    d = lm._snapshot_delta(PREV, cur)
    assert d == "~ [e3] AXCheckBox val=1"

def test_snapshot_delta_new_and_gone():
    cur = """=== App — focused window (snapshot #4) ===
[e1] AXWindow "Motion"
  [e2] AXStaticText val='Reduce motion'
  [e3] AXCheckBox val=0
  [e5] AXStaticText val='Saved'"""
    d = lm._snapshot_delta(PREV, cur)
    assert "+ [e5] AXStaticText val='Saved'" in d
    assert "gone: e4" in d

def test_snapshot_delta_no_change_and_full_tree_cases():
    same = PREV.replace("snapshot #3", "snapshot #4")
    assert lm._snapshot_delta(PREV, same) == "(no visible change since the last view)"
    assert lm._snapshot_delta(None, same) is None                      # first view
    assert lm._snapshot_delta(PREV, same.replace('"Motion"', '"General"')) is None  # window changed
    mostly_new = """=== App — focused window (snapshot #4) ===
[e1] AXWindow "Motion"
  [e7] a
  [e8] b
  [e9] c"""
    assert lm._snapshot_delta(PREV, mostly_new) is None                # >50% churn -> full
