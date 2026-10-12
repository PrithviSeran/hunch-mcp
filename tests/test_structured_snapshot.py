import pytest
from hunch.cdp import CDPSession


def fixture(monkeypatch):
    s = CDPSession.__new__(CDPSession)
    s.registry = {}; s._counter = 0; s.snapshot_count = 0
    monkeypatch.setattr(s, '_follow_new_tab', lambda: None)
    monkeypatch.setattr(s, '_document_identity', lambda: ('target', 'frame', 'loader', 'https://example.com'))
    monkeypatch.setattr(s, 'title', lambda: 'Test')
    monkeypatch.setattr(s, 'url', lambda: 'https://example.com')
    nodes = [{'nodeId': '1', 'role': {'value': 'button'}, 'name': {'value': '"quoted" … '+ 'x'*200},
              'value': {'value': 'y'*180}, 'backendDOMNodeId': 42,
              'properties': [{'name': 'expanded', 'value': {'value': True}}], 'childIds': ['2']},
             {'nodeId': '2', 'parentId': '1', 'role': {'value': 'link'}, 'name': {'value': 'Child'}, 'backendDOMNodeId': 43}]
    monkeypatch.setattr(s, '_cmd', lambda *args: {'nodes': nodes})
    return s


def test_lossless_and_explicit_budget(monkeypatch):
    s = fixture(monkeypatch)
    result, _ = s.snapshot(structured=True)
    assert result['complete']
    assert result['elements'][0]['name'] == '"quoted" … '+ 'x'*200
    assert result['elements'][0]['properties'] == {'expanded': True, 'value': 'y'*180}
    assert result['elements'][1]['depth'] == 1
    assert s.registry[result['elements'][1]['ref']] == 43
    result, _ = s.snapshot(structured=True, max_nodes=1)
    assert not result['complete'] and result['omitted'] == 1


def test_navigation_during_observation_invalidates_refs(monkeypatch):
    s = fixture(monkeypatch)
    identities = iter(['before', 'after'])
    monkeypatch.setattr(s, '_document_identity', lambda: next(identities))
    with pytest.raises(RuntimeError, match='document changed'):
        s.snapshot(structured=True)
    assert not s.registry
