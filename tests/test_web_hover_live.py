"""Opt-in, synthetic headless Chrome test; never attaches to a personal profile.

HUNCH_RUN_LIVE_HOVER=1 PYTHONPATH=src python -m pytest tests/test_web_hover_live.py
"""
import os
from pathlib import Path
import re
import subprocess
import time

import pytest

from hunch.cdp import CDPComputer, CDPSession


@pytest.mark.skipif(os.environ.get('HUNCH_RUN_LIVE_HOVER') != '1', reason='explicit live hover opt-in required')
def test_hover_reveals_css_control_without_click(tmp_path):
    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not chrome.exists():
        pytest.skip('Google Chrome not installed')
    profile = tmp_path / 'synthetic-profile'
    profile.mkdir()
    fixture = tmp_path / 'hover.html'
    fixture.write_text('''<!doctype html><title>Synthetic hover test</title>
        <style>
        body { margin: 120px; }
        #row { width: 250px; padding: 20px; }
        #reveal { visibility: hidden; }
        #row:hover #reveal { visibility: visible; }
        </style>
        <div id="row">
          <button>Account</button><button id="reveal">Preferences</button>
        </div>
        <script>window.clicks = 0;
          document.addEventListener('click', () => window.clicks++);
        </script>''')
    process = subprocess.Popen([str(chrome), '--headless=new', '--remote-debugging-port=0',
        '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
        f'--user-data-dir={profile}', fixture.as_uri()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = None
    try:
        deadline = time.monotonic() + 15
        port_file = profile / 'DevToolsActivePort'
        while not port_file.exists():
            assert process.poll() is None, 'headless Chrome exited'
            assert time.monotonic() < deadline, 'headless Chrome did not expose its endpoint'
            time.sleep(.05)
        port = int(port_file.read_text().splitlines()[0])
        session = CDPSession(port).connect()
        while True:
            snapshot = session.snapshot()[0]
            anchor = re.search(r'\[(e\d+)\] button "Account"', snapshot)
            if anchor:
                break
            assert time.monotonic() < deadline, 'synthetic page did not render: ' + snapshot
            time.sleep(.05)
        assert '"Preferences"' not in snapshot
        computer = CDPComputer.__new__(CDPComputer)
        computer.session, computer.identity = session, {'pid': process.pid, 'name': 'headless fixture'}
        receipt = computer.act([{'action': 'hover', 'ref': anchor.group(1)}], detailed=True)
        assert receipt['status'] == 'performed_unverified'
        assert receipt['disturbances'] == {}
        assert 'button "Preferences"' in receipt['observation']
        assert session._cmd('Runtime.evaluate', {'expression': 'window.clicks', 'returnByValue': True})['result']['value'] == 0
        assert session._cmd('Runtime.evaluate', {'expression': 'document.querySelector("#row").matches(":hover")',
                                               'returnByValue': True})['result']['value'] is True
    finally:
        if session and session.ws:
            session.ws.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
