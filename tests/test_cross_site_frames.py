"""Cross-site child frames: their tree is read over the frame's own target, a click goes to the
page's input at the frame element's offset, and nothing is sent when the frame or page changed
or something covers the frame. No browser or socket is opened."""
import json

import pytest

from hunch import cdp

PAGE = "page-1"
FRAME = "frame-9"
FRAME_URL = "https://consent.example.net/banner"
TARGETS = [
    {"type": "page", "id": PAGE, "url": "https://app.example.com/", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-1"},
    {"type": "iframe", "id": FRAME, "parentId": PAGE, "url": FRAME_URL,
     "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/frame-9"},
    {"type": "iframe", "id": "other", "parentId": "page-2", "url": "https://elsewhere.example.org/",
     "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/other"},
]
FRAME_TREE = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Consent"}, "backendDOMNodeId": 1, "childIds": ["2", "3", "4"]},
    {"nodeId": "2", "parentId": "1", "role": {"value": "StaticText"}, "name": {"value": "We use cookies."}, "backendDOMNodeId": 2},
    {"nodeId": "3", "parentId": "1", "role": {"value": "button"}, "name": {"value": "Reject all"}, "backendDOMNodeId": 30},
    {"nodeId": "4", "parentId": "1", "role": {"value": "button"}, "name": {"value": "Accept all"}, "backendDOMNodeId": 40,
     "properties": [{"name": "disabled", "value": {"value": True}}]},
]


class FakeWS:
    """The frame target's websocket: answers its commands, records them."""

    def __init__(self, log):
        self.log, self.queue = log, []

    def send(self, raw):
        m = json.loads(raw)
        self.log.append(("frame", m["method"], m["params"]))
        result = {}
        if m["method"] == "Accessibility.getFullAXTree":
            result = {"nodes": FRAME_TREE}
        elif m["method"] == "DOM.getBoxModel":
            result = {"model": {"content": [10, 10, 110, 10, 110, 40, 10, 40]}}   # centre (60, 25) in the frame
        self.queue.append(json.dumps({"id": m["id"], "result": result}))

    def recv(self):
        return self.queue.pop(0)

    def close(self):
        pass


def session(monkeypatch, hit_owner=True, targets=TARGETS):
    value = cdp.CDPSession.__new__(cdp.CDPSession)
    value.port, value.target_id, value.registry = 9222, PAGE, {"e1": 5}
    value._observed_document = (PAGE, "frame-1", "loader-1", "https://app.example.com/")
    calls = []
    state = {"targets": targets}

    def command(method, params=None):
        calls.append(("page", method, params))
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 77}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [200, 300, 600, 300, 600, 500, 200, 500]}}    # the <iframe> at (200, 300)
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1", "loaderId": "loader-1", "url": "https://app.example.com/"}}}
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "hit"}}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 77 if hit_owner else 12}}
        return {}

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self):
            return json.dumps(self.data).encode()

    monkeypatch.setattr(value, "_cmd", command)
    monkeypatch.setattr(cdp.urllib.request, "urlopen", lambda url, timeout=3: Resp(state["targets"]))
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: FakeWS(calls))
    return value, calls, state


def test_child_frames_are_the_bound_pages_cross_site_frames(monkeypatch):
    value, _, _ = session(monkeypatch)
    assert value.child_frames() == [{"target_id": FRAME, "url": FRAME_URL,
                                     "origin": "https://consent.example.net", "owner": 77}]


def test_frame_snapshot_reads_the_frames_own_tree_with_frame_refs(monkeypatch):
    value, calls, _ = session(monkeypatch)
    snap = value.frame_snapshot(FRAME)
    assert snap["origin"] == "https://consent.example.net"
    assert [(n["ref"], n["role"], n["name"], n["disabled"]) for n in snap["nodes"]] == [
        ("f1e1", "StaticText", "We use cookies.", False), ("f1e2", "button", "Reject all", False),
        ("f1e3", "button", "Accept all", True)]
    assert '[f1e2] button "Reject all"' in snap["text"] and '[f1e3] button "Accept all" disabled' in snap["text"]
    assert value.registry == {"e1": 5}                      # the page's refs are untouched
    assert ("frame", "Accessibility.getFullAXTree", {}) in calls


def test_frame_click_sends_page_input_at_the_frames_offset(monkeypatch):
    value, calls, _ = session(monkeypatch)
    value.frame_snapshot(FRAME)
    said = value.frame_click(FRAME, "f1e2")
    assert said == "clicked f1e2 in the frame from https://consent.example.net"
    mouse = [p for who, m, p in calls if m == "Input.dispatchMouseEvent"]
    assert [(p["type"], p["x"], p["y"]) for p in mouse] == [("mousePressed", 260, 325), ("mouseReleased", 260, 325)]
    assert all(who == "page" for who, m, _ in calls if m.startswith("Input."))
    assert not any(m in {"Page.bringToFront", "Input.dispatchKeyEvent"} for _, m, _ in calls)


def test_a_covered_frame_is_refused_without_input(monkeypatch):
    value, calls, _ = session(monkeypatch, hit_owner=False)
    value.frame_snapshot(FRAME)
    assert value.frame_click(FRAME, "f1e2").startswith("REFUSED: something in the page covers")
    assert not any(m.startswith("Input.") for _, m, _ in calls)


def test_a_frame_that_navigated_is_refused_without_input(monkeypatch):
    value, calls, state = session(monkeypatch)
    value.frame_snapshot(FRAME)
    state["targets"] = [dict(t, url="https://consent.example.net/other") if t["id"] == FRAME else t for t in TARGETS]
    assert value.frame_click(FRAME, "f1e2").startswith("REFUSED: the frame's document changed")
    assert not any(m.startswith("Input.") for _, m, _ in calls)


def test_a_page_that_changed_is_refused_without_input(monkeypatch):
    value, calls, _ = session(monkeypatch)
    value.frame_snapshot(FRAME)
    value._observed_document = (PAGE, "frame-1", "loader-2", "https://app.example.com/")
    with pytest.raises(RuntimeError, match="document changed"):
        value.frame_click(FRAME, "f1e2")
    assert not any(m.startswith("Input.") for _, m, _ in calls)


def test_stale_refs_and_frames_of_other_pages_are_refused(monkeypatch):
    value, calls, _ = session(monkeypatch)
    value.frame_snapshot(FRAME)
    with pytest.raises(KeyError, match="stale frame ref"):
        value.frame_click(FRAME, "f1e9")
    with pytest.raises(KeyError, match="no cross-site frame"):
        value.frame_snapshot("other")                     # a frame of another page
    assert value.frame_click(FRAME, "").startswith("REFUSED")
    assert not any(m.startswith("Input.") for _, m, _ in calls)


def test_a_frame_websocket_outside_the_local_endpoint_is_refused(monkeypatch):
    far = [dict(t, webSocketDebuggerUrl="ws://10.0.0.5:9222/devtools/page/frame-9") if t["id"] == FRAME else t
           for t in TARGETS]
    value, _, _ = session(monkeypatch, targets=far)
    with pytest.raises(RuntimeError, match="outside the verified local endpoint"):
        value.frame_snapshot(FRAME)
