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


def test_two_app_coexistence():
    """Two app_ids on one Mac: disjoint ports, profiles, keychain names — structurally
    unable to kill each other's browsers or read each other's credentials. A personal
    instance (no app_id) keeps every legacy name."""
    import hunch.auth as auth
    import hunch.creds as credmod
    a = Hunch(check_permissions=False, confirm="off", app_id="com.acme.mailbot")
    b = Hunch(check_permissions=False, confirm="off", app_id="com.other.invoicebot")
    p = Hunch(check_permissions=False, confirm="off")
    # CDP: distinct derived ports in 9400-9899, distinct profiles; personal keeps legacy
    assert a.web.port != b.web.port and 9400 <= a.web.port < 9900
    assert a.web.profile != b.web.profile
    assert "com.acme.mailbot" in a.web.profile
    assert p.web.port == 9337 and p.web.profile is None
    # stable across constructions (an app must find its own state again)
    assert Hunch(check_permissions=False, confirm="off",
                 app_id="com.acme.mailbot").web.port == a.web.port
    # keychain namespacing: token slot + credential service
    assert auth._token_service("com.acme.mailbot") != auth._token_service("com.other.invoicebot")
    assert credmod._service("com.acme.mailbot") != credmod._service("com.other.invoicebot")
    assert credmod._service(None) == "com.hunch.credentials"
    # metadata files live under the app dir
    assert "/apps/com.acme.mailbot/" in credmod._meta_path("creds_index.json", "com.acme.mailbot")
    # derived app_name; explicit overrides
    assert a.app_name == "Mailbot"
    assert Hunch(check_permissions=False, confirm="off", app_id="com.acme.mailbot",
                 app_name="Acme").app_name == "Acme"
    # bad app_id fails fast
    try:
        Hunch(check_permissions=False, confirm="off", app_id="not ok/slashes")
        assert False, "expected HunchError"
    except hunch.HunchError as e:
        assert "app_id" in str(e)


def test_notify_handler_routing():
    got = []
    h = Hunch(check_permissions=False, confirm="off", app_id="com.acme.mailbot",
              notify=lambda msg, title: got.append((msg, title)))
    h.notify("hello")
    assert got == [("hello", "Mailbot")]        # handler called, app_name title
    h.notify("urgent", "Custom title")
    assert got[-1] == ("urgent", "Custom title")
    try:
        Hunch(check_permissions=False, confirm="off", notify="not-a-callable")
        assert False, "expected HunchError"
    except hunch.HunchError as e:
        assert "notify" in str(e)


def test_protected_covers_app_dirs():
    assert gate.protected(os.path.expanduser("~/.hunch/apps/com.acme.mailbot/creds_index.json"))
    assert gate.protected(os.path.expanduser("~/.hunch/config.json"))
    assert not gate.protected(os.path.expanduser("~/Documents/x.txt"))


def test_creds_namespace_isolation(tmp_path, monkeypatch):
    """Namespaced metadata reads/writes never touch the personal files (fake HOME)."""
    import hunch.creds as credmod
    monkeypatch.setenv("HOME", str(tmp_path))
    credmod._write_index(["acme-github"], "com.acme.mailbot")
    assert credmod.list_services("com.acme.mailbot") == ["acme-github"]
    assert credmod.list_services() == []                        # personal store untouched
    assert credmod.list_services("com.other.invoicebot") == []  # other app blind to it
    credmod.set_domains("acme-github", ["github.com"], "com.acme.mailbot")
    assert credmod.domains_of("acme-github", "com.acme.mailbot") == ["github.com"]
    assert credmod.domains_of("acme-github") == []
    # gate.domain_mismatch consults the right namespace + brands the refusal
    msg = gate.domain_mismatch("acme-github", "https://evil.example.com",
                               app_name="Acme Mailbot", namespace="com.acme.mailbot")
    assert msg and msg.startswith("REFUSED") and "Acme Mailbot won't type" in msg
    assert gate.domain_mismatch("acme-github", "https://github.com",
                                namespace="com.acme.mailbot") is None


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


def test_shared_domain_mismatch_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(creds, "_meta_path",
                        lambda base, ns=None: str(tmp_path / (ns or "personal") / base))
    creds.set_domains("testsvc", ["google.com"])
    assert gate.domain_mismatch("testsvc", "https://accounts.google.com/signin") is None
    refusal = gate.domain_mismatch("testsvc", "https://evil.example.com/login")
    assert refusal and refusal.startswith("REFUSED")
    assert gate.domain_mismatch("unbound-svc", "https://anywhere.example") is None


def test_server_is_an_app_on_the_sdk():
    """The inversion invariants: one engine (every MCP tool name resolves in the shared
    dispatch table), one Hunch instance, personal policy + notify wrapper wired in."""
    import hunch.agent as agent_mod
    assert set(server.mcp._tool_manager._tools) == set(agent_mod._DISPATCH)
    assert isinstance(server._mac, Hunch)
    assert server._gate is server._mac._gate
    assert server._gate._policy == "personal"
    assert server._mac._notify_handler is server._notify


def test_server_run_delegation(monkeypatch):
    from mcp.server.fastmcp import Image as McpImage

    class FakeMac:
        def list_apps(self):
            return "Finder, Mail"

        def screenshot(self):
            return b"\x89PNG fake"

    monkeypatch.setattr(server, "_mac", FakeMac())
    assert server._run("list_apps") == "Finder, Mail"
    assert isinstance(server._run("screenshot"), McpImage)   # bytes -> MCP Image
    assert server._run("nope") == "unknown tool nope"


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
