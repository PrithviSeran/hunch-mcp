"""Hover uses renderer input, retains document guards, and never claims a reveal."""
import pytest
from jsonschema import Draft202012Validator, ValidationError

from hunch import cdp
from hunch.safari import SafariComputer
from hunch.tool_registry import catalog


def session(monkeypatch):
    value = cdp.CDPSession.__new__(cdp.CDPSession)
    value.registry = {"e1": 42}
    value.target_id = "renderer-1"
    value._observed_document = ("renderer-1", "frame-1", "loader-1", "https://example.com")
    calls = []

    def command(method, params=None):
        calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1", "loaderId": "loader-1", "url": "https://example.com"}}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [10, 20, 110, 20, 110, 60, 10, 60]}}
        return {}

    monkeypatch.setattr(value, "_cmd", command)
    return value, calls


def test_hover_moves_only_renderer_pointer(monkeypatch):
    value, calls = session(monkeypatch)
    assert value.hover("e1").startswith("hovered e1")
    assert [args for method, args in calls if method == "Input.dispatchMouseEvent"] == [
        {"type": "mouseMoved", "x": 60, "y": 40, "button": "none", "buttons": 0}]
    assert not any(method in {"Page.bringToFront", "Input.dispatchKeyEvent"} for method, _ in calls)
    assert sum(method == "Page.getFrameTree" for method, _ in calls) == 2
    assert ("DOM.scrollIntoViewIfNeeded", {"backendNodeId": 42}) in calls


@pytest.mark.parametrize("ref", [None, "", 5, "missing"])
def test_hover_invalid_refs_dispatch_no_input(monkeypatch, ref):
    value, calls = session(monkeypatch)
    if ref == "missing":
        with pytest.raises(KeyError, match="stale"):
            value.hover(ref)
    else:
        assert value.hover(ref).startswith("REFUSED")
    assert not any(method.startswith("Input.") for method, _ in calls)


@pytest.mark.parametrize("during_geometry", [False, True])
def test_hover_refuses_document_change_before_input(monkeypatch, during_geometry):
    value, calls = session(monkeypatch)
    if during_geometry:
        def changed_center(ref):
            value.target_id = "renderer-2"
            return 60, 40
        monkeypatch.setattr(value, "_center", changed_center)
    else:
        value.target_id = "renderer-2"
    with pytest.raises(RuntimeError, match="document changed"):
        value.hover("e1")
    assert value.registry == {}
    assert not any(method.startswith("Input.") for method, _ in calls)


def test_hover_invalid_geometry_dispatches_no_input(monkeypatch):
    value, calls = session(monkeypatch)
    monkeypatch.setattr(value, "_center", lambda ref: (float('nan'), 5))
    assert value.hover('e1').startswith('REFUSED')
    assert not any(method.startswith('Input.') for method, _ in calls)


def test_web_act_hover_receipt_is_unverified_and_focus_free(monkeypatch):
    value, calls = session(monkeypatch)
    computer = cdp.CDPComputer.__new__(cdp.CDPComputer)
    computer.session = value
    computer.identity = {"pid": 123, "name": "synthetic browser"}
    monkeypatch.setattr(computer, "snapshot", lambda: '[e2] button "Revealed"')
    monkeypatch.setattr(cdp.time, "sleep", lambda _: None)
    receipt = computer.act([{"action": "hover", "ref": "e1"}], detailed=True)
    assert receipt['status'] == 'performed_unverified'
    assert receipt['attempted_actions'] == 1
    assert receipt['unattempted_actions'] == 0
    assert receipt['disturbances'] == {}
    assert 'Revealed' in receipt['observation']


def test_failed_hover_stops_batch(monkeypatch):
    value, calls = session(monkeypatch)
    computer = cdp.CDPComputer.__new__(cdp.CDPComputer)
    computer.session, computer.identity = value, {}
    monkeypatch.setattr(computer, "snapshot", lambda: '')
    monkeypatch.setattr(cdp.time, "sleep", lambda _: None)
    receipt = computer.act([{'action': 'hover', 'ref': 'stale'}, {'action': 'click', 'ref': 'e1'}], detailed=True)
    assert receipt['status'] == 'blocked'
    assert receipt['unattempted_actions'] == 1
    assert not any(method.startswith('Input.') for method, _ in calls)


@pytest.mark.parametrize('detailed', [False, True])
def test_safari_hover_refuses_entire_batch_before_any_transport(detailed):
    computer = SafariComputer.__new__(SafariComputer)
    result = computer.act([{'action': 'click', 'ref': 'e1'}, {'action': 'hover', 'ref': 'e2'}], detailed=detailed)
    if detailed:
        assert result['status'] == 'blocked'
        assert result['attempted_actions'] == 0
        assert result['unattempted_actions'] == 2
    else:
        assert result.startswith('UNSUPPORTED:')


def test_hover_schema_is_web_only_and_requires_ref():
    tools = {tool['name']: tool['input_schema'] for tool in catalog()}
    web = Draft202012Validator(tools['web_act'])
    web.validate({'actions': [{'action': 'hover', 'ref': 'e1'}]})
    for action in ({'action': 'hover'}, {'action': 'hover', 'ref': ''}):
        with pytest.raises(ValidationError):
            web.validate({'actions': [action]})
    with pytest.raises(ValidationError):
        Draft202012Validator(tools['act']).validate({'actions': [{'action': 'hover', 'ref': 'e1'}]})
    assert len(tools) == 32
