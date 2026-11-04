"""`identity` reads what a page keeps for an element, in the main world, without input."""
import json
import shutil
import subprocess

import pytest

from hunch import cdp


def session(monkeypatch, value=None, object_id="obj-1"):
    s = cdp.CDPSession.__new__(cdp.CDPSession)
    s.registry = {"e1": 42}
    s.target_id = "renderer-1"
    s._observed_document = ("renderer-1", "frame-1", "loader-1", "https://example.com")
    calls = []

    def command(method, params=None):
        calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1", "loaderId": "loader-1", "url": "https://example.com"}}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": object_id}} if object_id else {}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "object", "value": value}}
        return {}

    monkeypatch.setattr(s, "_cmd", command)
    return s, calls


def test_identity_reads_keys_and_attrs_by_backend_id(monkeypatch):
    s, calls = session(monkeypatch, {"framework": "react", "keys": ["2026-11-18", "staff_member_1", 7282],
                                     "attrs": {"data-testid": "draggable-event"}})
    assert s.identity("e1") == {"framework": "react", "keys": ["2026-11-18", "staff_member_1", "7282"],
                                "attrs": {"data-testid": "draggable-event"}}
    assert ("DOM.resolveNode", {"backendNodeId": 42}) in calls
    call = next(p for m, p in calls if m == "Runtime.callFunctionOn")
    assert call["objectId"] == "obj-1" and call["returnByValue"] is True
    assert not any(m.startswith("Input.") or m == "Page.bringToFront" for m, _ in calls)


def test_identity_without_framework_is_empty_not_guessed(monkeypatch):
    s, _ = session(monkeypatch, {"framework": "none", "keys": [], "attrs": {"href": "/wiki/Main_Page"}})
    assert s.identity("e1") == {"framework": "none", "keys": [], "attrs": {"href": "/wiki/Main_Page"}}


@pytest.mark.parametrize("ref", [None, "", "e9"])
def test_identity_stale_or_missing_ref_sends_nothing(monkeypatch, ref):
    s, calls = session(monkeypatch)
    with pytest.raises(KeyError):
        s.identity(ref)
    assert calls == []


def test_identity_refuses_a_changed_document(monkeypatch):
    s, calls = session(monkeypatch)
    s.target_id = "renderer-2"
    with pytest.raises(RuntimeError, match="document changed"):
        s.identity("e1")
    assert not any(m == "Runtime.callFunctionOn" for m, _ in calls)


def test_identity_of_a_node_gone_from_the_page_fails_loudly(monkeypatch):
    s, _ = session(monkeypatch, object_id=None)
    with pytest.raises(RuntimeError, match="no longer on the page"):
        s.identity("e1")


def test_identity_unreadable_result_fails_loudly(monkeypatch):
    s, _ = session(monkeypatch, value="not an object")
    with pytest.raises(RuntimeError, match="could not be read"):
        s.identity("e1")


def test_identity_of_reads_by_backend_id_through_any_cmd():
    calls = []

    def cmd(method, params=None):
        calls.append((method, params))
        return {"object": {"objectId": "o"}} if method == "DOM.resolveNode" else \
            {"result": {"value": {"framework": "react", "keys": [1, "x"], "attrs": {}}}}

    assert cdp.identity_of(cmd, 7) == {"framework": "react", "keys": ["1", "x"], "attrs": {}}
    assert calls[0] == ("DOM.resolveNode", {"backendNodeId": 7})


def test_identities_reads_each_ref(monkeypatch):
    s, calls = session(monkeypatch, {"framework": "react", "keys": ["7"], "attrs": {}})
    s.registry["e2"] = 43
    out = s.identities(["e1", "e2"])
    assert set(out) == {"e1", "e2"} and out["e2"]["keys"] == ["7"]
    assert [p["backendNodeId"] for m, p in calls if m == "DOM.resolveNode"] == [42, 43]


# The function itself, run on fake DOM nodes under node: what the page sees is what counts.

NODE = shutil.which("node")

FAKE = r"""
const fn = %s;
const el = (attrs, extra) => Object.assign({
  nodeType: 1, _a: attrs,
  hasAttribute(a) { return a in this._a; }, getAttribute(a) { return this._a[a]; },
}, extra || {});
globalThis.location = {href: "https://app.example.com/admin#today"};
const fiber = (key, parent) => ({key, return: parent});
const root = fiber(null, null), day = fiber("2026-11-18", root), staff = fiber("staff_member_1", day),
      item = fiber(7282, staff), inner = fiber(null, item);
const react = el({"data-testid": "draggable-event", href: "https://app.example.com/calendar/event?x=1#top"},
                 {"__reactFiber$abc": inner});
const plain = el({id: "save"}, {});
const vueComp = {vnode: {key: "row-9"}, parent: {vnode: {key: null}, parent: null}};
const vue = el({}, {__vnode: {key: "cell-2"}, __vueParentComponent: vueComp});
const text = {nodeType: 3};
console.log(JSON.stringify([fn.call(react), fn.call(plain), fn.call(vue), fn.call(text)]));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_identity_function_reads_react_vue_and_plain_nodes():
    out = subprocess.run([NODE, "-e", FAKE % cdp.IDENTITY_FN], capture_output=True, text=True, check=True)
    react, plain, vue, text = json.loads(out.stdout)
    assert react == {"framework": "react", "keys": ["2026-11-18", "staff_member_1", "7282"],
                     "attrs": {"data-testid": "draggable-event", "href": "/calendar/event#top"}}
    assert plain == {"framework": "none", "keys": [], "attrs": {"id": "save"}}
    assert vue == {"framework": "vue", "keys": ["row-9", "cell-2"], "attrs": {}}
    assert text == {"framework": "none", "keys": [], "attrs": {}}
