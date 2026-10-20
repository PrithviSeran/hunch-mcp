"""Live Chrome smoke for the Linux web surface. Opt-in: HUNCH_LINUX_WEB_SMOKE=1."""
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HUNCH_LINUX_WEB_SMOKE") != "1" or sys.platform != "linux",
    reason="set HUNCH_LINUX_WEB_SMOKE=1 on Linux",
)

from hunch import Hunch  # noqa: E402
from hunch.cdp import launch_chromium, quit_cdp  # noqa: E402


PAGE = """<!doctype html><title>hunch-linux</title>
<button id="go">Go</button>
<input id="q" aria-label="Query">
<script>
document.getElementById('go').addEventListener('click', () => {
  document.body.dataset.clicked = 'yes';
});
</script>
"""


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(directory):
    handler = partial(SimpleHTTPRequestHandler, directory=directory)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _ref(snapshot, role, name):
    needle = f'{role} "{name}"'
    for line in snapshot.splitlines():
        if needle in line and line.strip().startswith("[e"):
            return line.strip().split()[0].strip("[]")
    raise AssertionError(f"no {needle} in snapshot:\n{snapshot}")


def test_chrome_open_act_screenshot_attach_and_quit(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    page_server = _serve(tmp_path)
    host, port = page_server.server_address
    url = f"http://{host}:{port}/index.html"
    mac = Hunch(confirm="off", check_permissions=False)
    second_owner = object()
    second_port = None
    try:
        opened = mac.web.open(url=url, isolated=True)
        assert "opened" in opened
        snap = mac.web.snapshot()
        button = _ref(snap, "button", "Go")
        field = _ref(snap, "textbox", "Query")
        mac.web.act([{"action": "click", "ref": button}])
        mac.web.act([{"action": "type", "ref": field, "text": "hello"}])
        mac.web.act([{"action": "key", "key": "a"}])
        shot = mac.web.screenshot()
        assert shot[:8] == b"\x89PNG\r\n\x1a\n"

        second_port = _free_port()
        launch_chromium("Google Chrome", second_port, url=url, isolated=True, owner=second_owner)
        other = Hunch(confirm="off", check_permissions=False)
        attached = other.web.attach("Google Chrome", second_port)
        if attached.get("status") == "blocked":
            match = next(item for item in attached["targets"] if "index.html" in (item.get("url") or ""))
            attached = other.web.attach("Google Chrome", second_port, target_id=match["id"])
        assert attached["status"] == "verified"
        quit_cdp(second_port, owner=second_owner)
        second_port = None
        quit_cdp(mac.web._computer.port, owner=mac.web._owner)
    finally:
        page_server.shutdown()
        if second_port is not None:
            try:
                quit_cdp(second_port, owner=second_owner)
            except Exception:
                pass
        computer = mac.web._computer
        if computer is not None:
            try:
                quit_cdp(computer.port, owner=mac.web._owner)
            except Exception:
                pass


def _hold_port(port):
    code = textwrap.dedent(f"""
        import socket, time
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("127.0.0.1", {port}))
        sock.listen(1)
        time.sleep(30)
    """)
    return subprocess.Popen([sys.executable, "-c", code])


def test_refusals_for_a_non_chrome_owner_and_two_listeners():
    mac = Hunch(confirm="off", check_permissions=False)
    port = _free_port()
    holder = _hold_port(port)
    try:
        time.sleep(0.3)
        with pytest.raises(RuntimeError, match="belongs to"):
            mac.web.attach("Google Chrome", port)
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    shared = _free_port()
    first, second = _hold_port(shared), _hold_port(shared)
    try:
        time.sleep(0.3)
        with pytest.raises(RuntimeError, match="cannot verify one owning process"):
            mac.web.attach("Google Chrome", shared)
    finally:
        for proc in (first, second):
            proc.terminate()
            proc.wait(timeout=5)
