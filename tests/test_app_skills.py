from types import SimpleNamespace

import pytest

from hunch.app_skills import page_skill
from hunch.sdk import Web


@pytest.mark.parametrize('url', ['https://docs.google.com/spreadsheets/d/1/edit',
    'https://docs.google.com.evil.example/document/d/1', 'https://example.com/document/d/1',
    'https://docs.google.com@evil.example/document/d/1', 'not-a-url'])
def test_other_pages_do_not_receive_docs_skill(url):
    assert page_skill(url) == ''


@pytest.mark.parametrize('backend', ['safari', 'cdp'])
def test_shared_snapshot_delivers_packaged_skill_after_navigation(backend):
    host = SimpleNamespace(_redact=lambda text: text)
    web = Web(host, 0)
    session = SimpleNamespace(url=lambda: 'https://example.com/')
    computer = SimpleNamespace(session=session, backend=backend)
    def snapshot():
        session.url = lambda: 'https://docs.google.com/document/d/1/edit#heading=h.1'
        return '[e1] editor'
    computer.snapshot = snapshot
    web._computer = computer
    result = web.snapshot()
    assert result.startswith('[e1] editor')
    assert page_skill(session.url()) in result
    computer.snapshot = lambda: 'BLOCKED: document changed'
    assert web.snapshot() == 'BLOCKED: document changed'
