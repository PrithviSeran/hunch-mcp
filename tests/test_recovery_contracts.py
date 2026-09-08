import json
from types import SimpleNamespace

import pytest

from hunch import Hunch, cdp, local_mac
from hunch.agent import _dispatch_core
from hunch.backends.codex import CodexBackend
from hunch.capabilities import Capabilities
from hunch.errors import ApprovalDenied, HunchError
from hunch.tool_registry import catalog


def test_inherited_endpoint_listener_parsing(monkeypatch):
    monkeypatch.setattr(cdp.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="p10\nR1\nf57\nd0xabc\nn127.0.0.1:9222\np20\nR10\nf57\nd0xabc\n"))
    assert cdp._endpoint_owner(cdp._endpoint_listeners(9222)) == 10


@pytest.mark.parametrize("listeners", [
    {},
    {10: {"parent": 1, "sockets": {"0xabc"}}, 20: {"parent": 1, "sockets": {"0xabc"}}},
    {10: {"parent": 1, "sockets": {"0xabc"}}, 20: {"parent": 10, "sockets": {"0xdef"}}},
    {10: {"parent": 1, "sockets": set()}, 20: {"parent": 10, "sockets": set()}},
    {10: {"parent": 20, "sockets": {"0xabc"}}, 20: {"parent": 10, "sockets": {"0xabc"}}},
])
def test_ambiguous_endpoint_owners_refused(listeners):
    assert cdp._endpoint_owner(listeners) is None


def test_inherited_endpoint_identity_rechecks_ownership(monkeypatch):
    listeners = {10: {"parent": 1, "sockets": {"0xabc"}},
                 20: {"parent": 10, "sockets": {"0xabc"}}}
    identity = {"pid": 10, "started_at": "original", "path": "/Applications/Test.app"}
    monkeypatch.setattr(cdp, "_endpoint_listeners", lambda _: listeners)
    monkeypatch.setattr(local_mac, "_running_identity", lambda _: identity)
    assert cdp.endpoint_identity(9222) == identity
    reads = iter([listeners, {}])
    monkeypatch.setattr(cdp, "_endpoint_listeners", lambda _: next(reads))
    with pytest.raises(RuntimeError, match="ownership changed"):
        cdp.endpoint_identity(9222)


def test_inherited_endpoint_refuses_pid_reuse(monkeypatch):
    listeners = {10: {"parent": 1, "sockets": {"0xabc"}},
                 20: {"parent": 10, "sockets": {"0xabc"}}}
    monkeypatch.setattr(cdp, "_endpoint_listeners", lambda _: listeners)
    reads = iter([{"pid": 10, "started_at": "old"}, {"pid": 10, "started_at": "new"}])
    monkeypatch.setattr(local_mac, "_running_identity", lambda _: next(reads))
    with pytest.raises(RuntimeError, match="ownership changed"):
        cdp.endpoint_identity(9222)




def test_host_background_constraint_cannot_be_weakened(monkeypatch):
    mac = Hunch(check_permissions=False, background_only=True, confirm="off")
    with pytest.raises(ApprovalDenied):
        mac.simultaneous = False
    with pytest.raises(ApprovalDenied):
        mac.focus_app("Finder")
    with pytest.raises(ApprovalDenied):
        mac.files.reveal(["/tmp/a"])
    assert mac.applescript('tell application "Finder" to activate').startswith("REFUSED")


def test_fallback_checks_policy_even_if_already_frontmost(monkeypatch):
    mac = Hunch(check_permissions=False, confirm=lambda request: False)
    session = mac._computer.session
    session._pid = 7
    monkeypatch.setattr(local_mac, "_frontmost", lambda: ("Fake", 7))
    monkeypatch.setattr(local_mac, "_mouse_click", lambda *a, **k: pytest.fail("denied input"))
    assert session.activate() is False


def test_cdp_refuses_unowned_reuse(monkeypatch):
    monkeypatch.setattr(cdp.urllib.request, "urlopen", lambda *a, **k: SimpleNamespace(read=lambda: b"{}"))
    monkeypatch.setattr(cdp, "verify_endpoint", lambda *a: {"pid": 1})
    monkeypatch.setattr(cdp, "_OWNED_ENDPOINTS", {})
    monkeypatch.setattr(cdp.subprocess, "run", lambda *a, **k: pytest.fail("unexpected launch"))
    with pytest.raises(RuntimeError, match="already in use"):
        cdp.launch_chromium("Loom", 9472)


def test_cdp_cannot_quit_unowned_port(monkeypatch):
    monkeypatch.setattr(cdp, "_OWNED_ENDPOINTS", {})
    with pytest.raises(RuntimeError, match="unowned"):
        cdp.quit_cdp(9472)


def test_recovery_plan_expires_on_pid_reuse(monkeypatch):
    mac = Hunch(check_permissions=False, confirm="off")
    recovery = mac._capabilities
    plan = recovery._plan({"pid": 1, "started_at": "old"}, "observe", "enable_ax")
    monkeypatch.setattr(local_mac, "_running_identity", lambda _: {"pid": 1, "started_at": "new"})
    with pytest.raises(HunchError, match="changed"):
        recovery.recover(plan["plan_id"])
    with pytest.raises(HunchError, match="already attempted"):
        recovery.recover(plan["plan_id"])


def test_recovery_refuses_changed_host_constraints(monkeypatch):
    mac = Hunch(check_permissions=False, confirm="off")
    identity = {"pid": 1}
    plan = mac._capabilities._plan(identity, "observe", "enable_ax")
    monkeypatch.setattr(local_mac, "_running_identity", lambda _: identity)
    mac.simultaneous = True
    with pytest.raises(HunchError, match="constraints changed"):
        mac.recover(plan["plan_id"])


def test_codex_serializes_instance_configuration():
    mac = Hunch(check_permissions=False, confirm="off", background_only=True,
                app_id="test.example", cdp_port=9891, cdp_profile="/tmp/test-profile")
    _, _, env = CodexBackend(mac)._mcp_server_command()
    config = json.loads(env["HUNCH_RUNTIME_CONFIG"])
    assert config["background_only"] and config["simultaneous"]
    assert config["cdp_port"] == 9891 and config["cdp_profile"] == "/tmp/test-profile"
    assert config["app_id"] == "test.example"


def test_codex_rejects_live_callback():
    mac = Hunch(check_permissions=False, confirm=lambda _: False)
    with pytest.raises(HunchError, match="live host callbacks"):
        CodexBackend(mac)._mcp_server_command()


def test_secret_echo_redaction_and_screenshot_refusal(monkeypatch):
    mac = Hunch(check_permissions=False)
    mac._secrets.add("synthetic-secret")
    monkeypatch.setattr(mac, "list_apps", lambda: "echo synthetic-secret")
    value, error = _dispatch_core(mac, "list_apps", {})
    assert not error and value == "echo [REDACTED]"
    with pytest.raises(HunchError, match="screenshot blocked"):
        mac.screenshot()
    with pytest.raises(HunchError, match="screenshot blocked"):
        mac.web.screenshot()


def test_cdp_never_follows_unrelated_popup(monkeypatch):
    session = cdp.CDPSession(9472)
    session.target_id = "main"
    session._known_targets = {"main"}
    monkeypatch.setattr(session, "_list_page_targets", lambda: [
        {"id": "helper", "openerId": "another"}, {"id": "main"}])
    monkeypatch.setattr(session, "_open_ws", lambda _: pytest.fail("followed unrelated target"))
    assert not session._follow_new_tab()


def test_ax_permission_denial_still_offers_cdp_attachment(monkeypatch):
    mac = Hunch(check_permissions=False)
    recovery = mac._capabilities
    monkeypatch.setattr(recovery, "targets", lambda _: {
        "target": {"pid": 17}, "trusted": False, "windows": []})
    monkeypatch.setattr(local_mac, "_proc_cmdline", lambda _: "App --remote-debugging-port=9222")
    monkeypatch.setattr(local_mac, "_embedded_chromium", lambda _: True)
    result = recovery.inspect("App")
    assert result["status"] == "permission_denied"
    assert [p["action"] for p in result["recoveries"]] == ["attach_cdp"]


def test_attachment_rejects_other_process_of_same_app(monkeypatch):
    mac = Hunch(check_permissions=False)
    monkeypatch.setattr(cdp, "verify_endpoint", lambda *a: {"pid": 8, "path": "/App.app"})
    with pytest.raises(HunchError, match="different process"):
        mac.web.attach("/App.app", 9222, expected_identity={"pid": 7, "path": "/App.app"})


def test_websocket_metadata_cannot_redirect_connection(monkeypatch):
    session = cdp.CDPSession(9222)
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda *a, **k: pytest.fail("network call"))
    for url in ("ws://example.com:9222/devtools/page/1", "ws://127.0.0.1:9223/devtools/page/1"):
        with pytest.raises(RuntimeError, match="outside"):
            session._open_ws({"webSocketDebuggerUrl": url})


def test_one_sdk_cannot_quit_another_sdk_browser(monkeypatch):
    first = Hunch(check_permissions=False)
    second = Hunch(check_permissions=False)
    monkeypatch.setattr(cdp, "_OWNED_ENDPOINTS", {9222: {"owner": first.web._owner}})
    monkeypatch.setattr(cdp, "endpoint_identity", lambda _: pytest.fail("must refuse before OS access"))
    with pytest.raises(RuntimeError, match="unowned"):
        cdp.quit_cdp(9222, owner=second.web._owner)


def test_secret_fill_does_not_fall_back_to_keyboard(monkeypatch):
    session = cdp.CDPSession(9222)
    session.registry["e1"] = 123
    calls = []
    def command(method, params=None):
        calls.append(method)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "field"}}
        return {"result": {"value": False}}
    monkeypatch.setattr(session, "validate_document", lambda: None)
    monkeypatch.setattr(session, "_cmd", command)
    assert not session.fill_secret("e1", "fixture-secret", "https://example.com/form")
    assert calls == ["DOM.resolveNode", "Runtime.callFunctionOn"]


def test_screen_approval_does_not_cross_targets():
    from hunch.gate import Gate
    permission = Gate(confirm=lambda _: True)
    permission.bind_screen_target(("pid:1", "start-one"))
    permission.mark_screen_approval()
    assert permission.screen_approved()
    permission.bind_screen_target(("pid:1", "start-two"))
    assert not permission.screen_approved()


@pytest.mark.parametrize("update_status,add_status,expected", [(0, 0, ["update"]),
                                                          (-25300, 0, ["update", "add"]),
                                                          (-25293, 0, ["update"])])
def test_keychain_writes_use_native_api_without_secret_argv(monkeypatch, update_status, add_status, expected):
    from hunch import creds
    calls = []
    def update(*args):
        calls.append("update")
        return update_status
    def add(*args):
        calls.append("add")
        return add_status
    monkeypatch.setattr(creds, "_security_framework", lambda: (
        SimpleNamespace(SecItemUpdate=update, SecItemAdd=add), lambda name: name))
    monkeypatch.setattr(creds.subprocess, "run", lambda *a, **k: pytest.fail("secret subprocess"))
    if update_status == -25293:
        with pytest.raises(RuntimeError, match="OSStatus -25293"):
            creds._store_keychain_blob("fixture", "account", "synthetic-secret")
    else:
        creds._store_keychain_blob("fixture", "account", "synthetic-secret")
    assert calls == expected


def test_installed_inventory_is_separate_and_bounded(tmp_path):
    import plistlib
    from hunch.targets import installed_apps
    for name in ("One", "Two"):
        contents = tmp_path / f"{name}.app" / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleName": name, "CFBundleIdentifier": f"test.{name}",
            "CFBundleShortVersionString": "1.2"}))
    result = installed_apps(roots=[str(tmp_path)], limit=1)
    assert result["truncated"] and result["scanned"] == 1
    assert result["installed"][0]["version"] == "1.2"
    assert "pid" not in result["installed"][0]


def test_login_retains_existing_editor_session(monkeypatch):
    mac = Hunch(check_permissions=False)
    session = SimpleNamespace(wait_ready=lambda: None, mark=lambda: None)
    computer = SimpleNamespace(editor=True, session=session)
    mac.web._computer = computer
    monkeypatch.setattr(mac.web, "_session", lambda: session)
    monkeypatch.setattr(cdp, "quit_cdp", lambda *a, **k: pytest.fail("login restarted editor"))
    assert "background" in mac.web.login()
    assert mac.web._computer is computer


def test_explicit_port_disables_automatic_allocation(monkeypatch):
    mac = Hunch(check_permissions=False)
    assert mac.web._auto_port
    mac.web.port = 9222
    assert not mac.web._auto_port
    assert mac.web._launch_port() == 9222


def test_model_arguments_cannot_self_approve(monkeypatch):
    mac = Hunch(check_permissions=False)
    monkeypatch.setattr(mac, "act", lambda *a, **k: pytest.fail("self-approved action"))
    result, error = _dispatch_core(mac, "act", {"actions": [], "confirm": True})
    assert not error and "additionalProperties" in result


def test_detailed_receipt_requires_explicit_readback_for_verification():
    from hunch.results import action_receipt
    args = (["pressed"], 1, "ax", {"pid": 1}, {})
    assert action_receipt(*args)["status"] == "performed_unverified"
    condition = {"ref": "e1", "field": "value", "equals": "wanted"}
    assert action_receipt(*args, condition, lambda *a: "different")["status"] == "performed_unverified"
    assert action_receipt(*args, condition, lambda *a: "wanted")["status"] == "verified"
    failed = action_receipt(["refused"], 2, "ax", {}, {}, condition,
                            lambda *a: pytest.fail("readback after blocked prerequisite"))
    assert failed["status"] == "blocked" and failed["unattempted_actions"] == 1


def test_native_detailed_action_verifies_field_after_dispatch(monkeypatch):
    mac = Hunch(check_permissions=False, background_only=True)
    computer = mac._computer
    monkeypatch.setattr(local_mac.time, "sleep", lambda _: None)
    monkeypatch.setattr(computer, "snapshot", lambda: "[e1] AXTextField")
    monkeypatch.setattr(computer.session, "set_text", lambda *a, **k: "set field")
    monkeypatch.setattr(computer.session, "_el", lambda _: object())
    monkeypatch.setattr(local_mac.ax, "read_attr", lambda *a: (0, "actual"))
    result = mac.act([{"action": "type", "ref": "e1", "text": "wanted"}], detailed=True,
                     postcondition={"ref": "e1", "field": "value", "equals": "wanted"})
    assert result["status"] == "performed_unverified"
    assert not result["postcondition"]["matched"]
    monkeypatch.setattr(local_mac.ax, "read_attr", lambda *a: (0, "wanted"))
    result = mac.act([{"action": "type", "ref": "e1", "text": "wanted"}], detailed=True,
                     postcondition={"ref": "e1", "field": "value", "equals": "wanted"})
    assert result["status"] == "verified" and result["disturbances"] == {}


def test_codex_child_applies_serialized_host_configuration():
    import os
    import subprocess
    import sys
    mac = Hunch(check_permissions=False, background_only=True, confirm="off",
                app_id="test.child", cdp_port=9789, cdp_profile="/tmp/child-profile")
    _, _, overrides = CodexBackend(mac)._mcp_server_command()
    code = ("import json; from hunch.server import _mac; "
            "print(json.dumps({'background':_mac.background_only, 'app_id':_mac.app_id, "
            "'port':_mac.web.port, 'profile':_mac.web.profile, 'simultaneous':_mac.simultaneous}))")
    child = subprocess.run([sys.executable, "-c", code], env={**os.environ, **overrides},
                           capture_output=True, text=True, check=True)
    assert json.loads(child.stdout) == {"background": True, "app_id": "test.child", "port": 9789,
                                       "profile": "/tmp/child-profile", "simultaneous": True}


def test_codex_does_not_drop_secret_protection_state():
    mac = Hunch(check_permissions=False)
    mac._secrets.add("fixture-secret")
    with pytest.raises(HunchError, match="secret-output protection"):
        CodexBackend(mac)._mcp_server_command()


def test_failed_graceful_cdp_quit_never_forces(monkeypatch):
    import AppKit
    owner = object()
    identity = {"pid": 123, "started_at": "fixture"}
    calls = []
    process = SimpleNamespace(terminate=lambda: calls.append("terminate"), isTerminated=lambda: False,
                              forceTerminate=lambda: pytest.fail("force quit"))
    monkeypatch.setattr(AppKit, "NSRunningApplication", SimpleNamespace(
        runningApplicationWithProcessIdentifier_=lambda _: process))
    monkeypatch.setattr(cdp, "_OWNED_ENDPOINTS", {9222: {"owner": owner, "identity": identity}})
    monkeypatch.setattr(cdp, "endpoint_identity", lambda _: identity)
    monkeypatch.setattr(local_mac, "_process_alive", lambda _: True)
    clock = iter([0, 7])
    monkeypatch.setattr(cdp.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not quit gracefully"):
        cdp.quit_cdp(9222, owner=owner)
    assert calls == ["terminate"] and 9222 in cdp._OWNED_ENDPOINTS


def test_ready_requested_workspace_does_not_wait_for_other_windows(monkeypatch):
    session = cdp.CDPSession(9222)
    pages = [{"id": "one", "title": "One", "url": "file:///workbench.html"},
             {"id": "two", "title": "Two", "url": "file:///workbench.html"}]
    monkeypatch.setattr(session, "_list_page_targets", lambda: pages)
    picked = []
    monkeypatch.setattr(session, "_open_ws", lambda page: picked.append(page["id"]))
    monkeypatch.setattr(cdp.time, "sleep", lambda _: pytest.fail("unnecessary renderer wait"))
    session.connect(editor=True, folder="/tmp/Two")
    assert picked == ["two"]


def test_duplicate_workspace_titles_require_selection():
    pages = [{"id": "one", "title": "Same", "url": "file:///workbench.html"},
             {"id": "two", "title": "Same", "url": "file:///workbench.html"}]
    assert cdp._pick_workbench(pages, "/tmp/Same") is None


def test_quit_uses_kernel_liveness_instead_of_cached_app_flag(monkeypatch):
    process = SimpleNamespace(terminate=lambda: None, isTerminated=lambda: False)
    monkeypatch.setattr(local_mac, "_resolve_app", lambda _: {"pid": 123})
    monkeypatch.setattr(local_mac, "NSRunningApplication", SimpleNamespace(
        runningApplicationWithProcessIdentifier_=lambda _: process))
    monkeypatch.setattr(local_mac, "_process_alive", lambda _: False)
    monkeypatch.setattr(local_mac.time, "sleep", lambda _: None)
    assert local_mac.quit_app("Fixture") == "quit Fixture"
