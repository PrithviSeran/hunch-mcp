import subprocess
import sys

import pytest

from hunch.errors import HunchError
from hunch.safari_input import _window_for_url


def response(monkeypatch, stdout, stderr='', code=0):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)
    monkeypatch.setattr('hunch.safari_input.subprocess.run', run)
    return calls


def test_unique_selected_match_and_url_argument(monkeypatch):
    calls = response(monkeypatch, '1,3853\n')
    url = 'https://example.com/document?q="quoted"#section'
    assert _window_for_url(url) == 3853
    assert calls[0][-1] == url
    assert url not in calls[0][2]


@pytest.mark.parametrize('stdout,message', [
    ('0,0', 'No Safari tab matches'),
    ('2,3853', 'Multiple Safari tabs'),
    ('1,0', 'not selected'),
    ('invalid', 'invalid response'),
])
def test_lookup_failures_are_distinct(monkeypatch, stdout, message):
    response(monkeypatch, stdout)
    with pytest.raises(HunchError, match=message):
        _window_for_url('https://example.com/document')


def test_applescript_error_is_not_misreported_as_duplicate_tabs(monkeypatch):
    response(monkeypatch, '', 'Not authorized to send Apple events to Safari. (-1743)', 1)
    with pytest.raises(HunchError, match=r'lookup failed.*-1743'):
        _window_for_url('https://example.com/document')


def test_lookup_timeout(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)
    monkeypatch.setattr('hunch.safari_input.subprocess.run', run)
    with pytest.raises(HunchError, match='lookup timed out'):
        _window_for_url('https://example.com/document')


@pytest.mark.skipif(sys.platform != 'darwin', reason='AppleScript interpreter is macOS-only')
def test_real_applescript_fragment_normalization_without_app_access(monkeypatch):
    # Execute the actual URL handler with the native interpreter, but omit the
    # Safari block: this needs neither a browser nor Automation permission.
    real_run = subprocess.run
    calls = response(monkeypatch, '1,3853')
    _window_for_url('https://example.com/')
    handler = calls[0][2].split('on run argv', 1)[0]
    script = handler + '''on run argv
return my documentURL(item 1 of argv)
end run'''
    for url, expected in [
        ('https://docs.google.com/document/d/test/edit?tab=t.0#heading=h.abc',
         'https://docs.google.com/document/d/test/edit?tab=t.0'),
        ('https://example.com/CaseSensitive?x=%23value#fragment',
         'https://example.com/CaseSensitive?x=%23value'),
        ('https://example.com/no-fragment', 'https://example.com/no-fragment'),
    ]:
        result = real_run(['/usr/bin/osascript', '-e', script, url],
                          capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
