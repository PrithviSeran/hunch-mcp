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


def test_gate_env_override_personal_only():
    """The env kill-switch (HUNCH_NO_INTERNAL_GATE) belongs to the PERSONAL app
    (policy='personal', what the MCP server passes). A default instance-owned Gate
    ignores it — another process's settings must never disarm an SDK app's gates."""
    old = os.environ.get("HUNCH_NO_INTERNAL_GATE")
    os.environ["HUNCH_NO_INTERNAL_GATE"] = "1"
    try:
        assert not gate.Gate(confirm="dialog", policy="personal").enabled("shell")
        assert gate.Gate(confirm="dialog").enabled("shell")   # uniform default: unaffected
    finally:
        if old is None:
            del os.environ["HUNCH_NO_INTERNAL_GATE"]
        else:
            os.environ["HUNCH_NO_INTERNAL_GATE"] = old


def test_gate_instance_policy_forms():
    # None -> every gate on
    g = gate.Gate(confirm="dialog")
    assert all(g.enabled(c) for c in policy.DEFAULT_GATES)
    # dict -> instance-owned, per-category with default True, auto_approve_all wins
    g = gate.Gate(confirm="dialog", policy={"gates": {"shell": False}})
    assert not g.enabled("shell") and g.enabled("focus_steal")
    g = gate.Gate(confirm="dialog", policy={"auto_approve_all": True})
    assert not g.enabled("shell")
    # callable -> custom resolver
    g = gate.Gate(confirm="dialog", policy=lambda c: c == "shell")
    assert g.enabled("shell") and not g.enabled("focus_steal")
    # bad form fails fast
    try:
        gate.Gate(confirm="dialog", policy=42)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_gate_confirm_callable():
    from hunch.errors import ConsentRequest
    seen = []
    g = gate.Gate(confirm=lambda req: seen.append(req) or True, app_name="Acme Mailbot")
    assert g.confirm_dialog("Acme Mailbot wants to bring “Mail” to the front. Allow?",
                            category="app_to_front", detail="Mail") is True
    assert isinstance(seen[0], ConsentRequest)
    assert seen[0].category == "app_to_front" and seen[0].detail == "Mail"
    assert "Acme Mailbot" in seen[0].prompt
    # deny path
    assert gate.Gate(confirm=lambda req: False).confirm_dialog("x?") is False
    # a BROKEN callback fails closed
    def boom(req):
        raise RuntimeError("ui crashed")
    assert gate.Gate(confirm=boom).confirm_dialog("x?") is False
    # confirm="off" auto-approves without any UI
    assert gate.Gate(confirm="off").confirm_dialog("x?") is True


def test_gate_branding_in_dialogs():
    seen = []
    g = gate.Gate(confirm=lambda req: seen.append(req) or False, app_name="Acme Mailbot")
    msg = g.front_gate("Some App That Is Not Frontmost")
    assert msg is not None and "did NOT approve" in msg
    assert seen and seen[0].prompt.startswith("Acme Mailbot wants to bring")
    assert seen[0].category == "app_to_front"


def test_hunch_policy_validation():
    try:
        Hunch(check_permissions=False, confirm="off", policy={"gates": {"warp_drive": True}})
        assert False, "expected HunchError"
    except hunch.HunchError as e:
        assert "warp_drive" in str(e)
    h = Hunch(check_permissions=False, confirm="off", app_name="Acme Mailbot")
    assert h.app_name == "Acme Mailbot" and h._gate.app_name == "Acme Mailbot"


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
