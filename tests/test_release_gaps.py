"""Failure-boundary checks added during final capability acceptance."""
from types import SimpleNamespace

import pytest

from hunch import Hunch, cdp, local_mac
from hunch.errors import ApprovalDenied


def test_runtime_release_excludes_learning():
    from hunch.tool_registry import catalog
    from hunch.agent import _dispatch_core
    names = {tool["name"] for tool in catalog()}
    assert len(names) == 32
    assert {"app_target", "app_capabilities", "app_recover"} <= names
    assert not {"app_operations", "app_invoke"} & names
    assert not any(name.startswith("learning_") for name in names)
    mac = Hunch(check_permissions=False)
    assert not hasattr(mac, "learning")
    assert _dispatch_core(mac, "learning_build", {}) == ("unknown tool learning_build", True)


@pytest.mark.parametrize("front,expected", [(7, "focused"), (9, "UNVERIFIED")])
@pytest.mark.parametrize("app_missing", [False, True])
def test_activation_fallback_requires_foreground_readback(monkeypatch, front, expected, app_missing):
    monkeypatch.setattr(local_mac, "_resolve_app", lambda _: {"pid": 7})
    monkeypatch.setattr(local_mac, "_announce_front", lambda _: None)
    monkeypatch.setattr(local_mac, "NSRunningApplication", SimpleNamespace(
        runningApplicationWithProcessIdentifier_=lambda _: None if app_missing else SimpleNamespace(activateWithOptions_=lambda _: False)))
    called = []
    monkeypatch.setattr(local_mac, "_activate_process", lambda pid: called.append(pid) or True)
    monkeypatch.setattr(local_mac, "_frontmost", lambda: ("app", front))
    monkeypatch.setattr(local_mac.time, "sleep", lambda _: None)
    assert local_mac.focus_app("test").startswith(expected)
    assert called == [7]


def test_denied_activation_never_reaches_fallback(monkeypatch):
    from hunch import gate, sdk
    # CI can start with Finder foreground, which legitimately skips a focus gate.
    # This case must request a real switch without reading or changing the desktop.
    monkeypatch.setattr(gate, "_frontmost", lambda: ("Other app", 99))
    monkeypatch.setattr(sdk, "_focus_app", lambda _: pytest.fail("denied native activation"))
    monkeypatch.setattr(local_mac, "_activate_process", lambda _: pytest.fail("denied activation"))
    with pytest.raises(ApprovalDenied):
        Hunch(check_permissions=False, background_only=True, confirm="off").focus_app("Finder")
    with pytest.raises(ApprovalDenied):
        Hunch(check_permissions=False, confirm=lambda _: False).focus_app("Finder")


@pytest.mark.parametrize("failure", ["port_claimed", "timeout", "profile_changed"])
def test_failed_launch_does_not_register_ownership(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(cdp, "_OWNED_ENDPOINTS", {})
    monkeypatch.setattr(cdp, "_resolve_app", lambda _: "Test")
    def unavailable(*a, **k):
        raise OSError("no listener yet")
    monkeypatch.setattr(cdp.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(cdp.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    def wait(*a, **k):
        if failure == "timeout":
            raise RuntimeError("debug port did not open")
    monkeypatch.setattr(cdp, "_wait_for_port", wait)
    def verify(*a):
        if failure == "port_claimed":
            raise RuntimeError("port belongs to another app")
        return {"pid": 7}
    monkeypatch.setattr(cdp, "verify_endpoint", verify)
    monkeypatch.setattr(local_mac, "_proc_cmdline", lambda _: "Test --user-data-dir=/wrong-profile")
    with pytest.raises(RuntimeError):
        cdp.launch_chromium("Test", 9222, profile=str(tmp_path))
    assert cdp._OWNED_ENDPOINTS == {}


@pytest.mark.parametrize("readback,requested,expected", [("wrong", 1, "performed_unverified"),
                                                       ("wanted", 2, "performed_unverified"),
                                                       ("wanted", 1, "verified")])
def test_postcondition_does_not_hide_mismatch_or_incomplete_batch(readback, requested, expected):
    from hunch.results import action_receipt
    receipt = action_receipt(["clicked e1"], requested, "cdp", {}, {},
                             {"ref": "e1", "field": "value", "equals": "wanted"},
                             lambda *a: readback)
    assert receipt["status"] == expected
