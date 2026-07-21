"""Dependency-light smoke tests for the Python SDK (`from hunch import Hunch`).

Run: .venv/bin/python -m pytest tests/  (or plain `python tests/test_sdk_smoke.py`).
Needs pyobjc but touches no Keychain items and no UI; the constructor's Accessibility
check is bypassed with check_permissions=False so these run on any machine/CI.
"""

import os
import subprocess
import sys

import hunch
import hunch.creds as creds
import hunch.gate as gate
import hunch.policy as policy
import hunch.server as server
from hunch.sdk import Hunch
from hunch.gate import ApprovalDenied, WebNotOpen  # noqa: F401 (import = part of the test)


def _client():
    return Hunch(check_permissions=False, confirm="off")


def test_bare_import_is_dependency_free():
    # Must run in a fresh interpreter: THIS process already imported the server (pyobjc etc.).
    code = ("import hunch, sys; "
            "heavy = [m for m in ('AppKit','objc','ApplicationServices','Quartz','mcp','websocket') "
            "if m in sys.modules]; "
            "assert not heavy, f'bare import pulled {heavy}'; "
            "print(hunch.__version__)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=os.path.join(os.path.dirname(__file__), os.pardir, "src"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == hunch.__version__


def test_lazy_exports_resolve():
    for name in ("Hunch", "HunchError", "ApprovalDenied", "AccessibilityNotGranted",
                 "WebNotOpen", "StaleRef"):
        assert getattr(hunch, name), name
        assert name in dir(hunch), name


def test_gate_confirm_off_disables_every_category():
    g = gate.Gate(confirm="off")
    for cat in policy.DEFAULT_GATES:
        assert not g.enabled(cat), cat


def test_gate_env_override_wins():
    old = os.environ.get("HUNCH_NO_INTERNAL_GATE")
    os.environ["HUNCH_NO_INTERNAL_GATE"] = "1"
    try:
        assert not gate.Gate(confirm="dialog").enabled("shell")
    finally:
        if old is None:
            del os.environ["HUNCH_NO_INTERNAL_GATE"]
        else:
            os.environ["HUNCH_NO_INTERNAL_GATE"] = old


def test_gate_rejects_bad_confirm():
    try:
        gate.Gate(confirm="yolo")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_screen_approval_is_per_instance():
    a, b = gate.Gate(), gate.Gate()
    a.mark_screen_approval()
    try:
        assert a.screen_approved()
        assert not b.screen_approved(), "approval must not leak across Gate instances"
    finally:
        a._screen_ok_until = 0.0
        import hunch.local_mac as local_mac
        local_mac._suppress_until = 0.0   # undo the notice-suppression side effect


def test_web_not_open_raises():
    h = _client()
    for call in (h.web.snapshot, h.web.tabs, h.web.screenshot,
                 lambda: h.web.act([]), lambda: h.web.switch_tab(0),
                 lambda: h.web.fill_login("x"), lambda: h.web.fill_secret("x")):
        try:
            call()
            assert False, "expected WebNotOpen"
        except WebNotOpen:
            pass
    h.close()   # close with no session open is a no-op, not an error


def test_files_protected_paths_refused():
    h = _client()
    assert h.files.trash("~/.hunch/config.json").startswith("REFUSED")
    assert h.files.trash(["~/Desktop/x", "~/.hunch"]).startswith("REFUSED")
    assert h.files.move("~/.hunch/creds_index.json", "/tmp/x").startswith("REFUSED")
    assert h.files.copy("/tmp/x", "~/.hunch/y").startswith("REFUSED")
    assert h.files.mkdir("~/.hunch/sub").startswith("REFUSED")


def test_shared_domain_mismatch_guard():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        old_domains = creds._DOMAINS
        creds._DOMAINS = os.path.join(d, "creds_domains.json")
        try:
            creds.set_domains("testsvc", ["google.com"])
            assert gate.domain_mismatch("testsvc", "https://accounts.google.com/signin") is None
            refusal = gate.domain_mismatch("testsvc", "https://evil.example.com/login")
            assert refusal and refusal.startswith("REFUSED")
            assert gate.domain_mismatch("unbound-svc", "https://anywhere.example") is None
        finally:
            creds._DOMAINS = old_domains


def test_server_delegates_to_gate():
    # Regression guard for the extraction: server aliases must point at the shared layer.
    assert server._protected is gate.protected
    assert server._as_str is gate.as_str
    assert isinstance(server._gate, gate.Gate)
    assert server._gate.confirm == "dialog"


def test_simultaneous_property():
    h = _client()
    assert h.simultaneous is False
    h.simultaneous = True
    assert h._computer.simultaneous is True


if __name__ == "__main__":
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        getattr(mod, name)()
        print(f"ok {name}")
    print("all SDK smoke tests passed")
