from types import SimpleNamespace

import pytest

from hunch import local_mac, sdk
from hunch.targets import TargetError, TargetRegistry, select_running


def test_duplicate_identity_requires_pid():
    apps = [{"pid": p, "name": "Loom", "bundle_id": "com.loom", "path": "/Applications/Loom.app"}
            for p in (1, 2)]
    for query in ("Loom", "com.loom", "/Applications/Loom.app"):
        with pytest.raises(TargetError, match="pid:1.*pid:2"):
            select_running(query, apps)
    assert select_running("pid:2", apps)["pid"] == 2


def test_window_handles_expire_on_process_restart():
    registry = TargetRegistry()
    element = object()
    first = registry.bind({"pid": 1, "started_at": "a"}, [(element, ["AXFocusedWindow"])])[0]
    registry.bind({"pid": 1, "started_at": "b"}, [(element, ["AXMainWindow"])])
    with pytest.raises(TargetError, match="stale"):
        registry.window(first.handle)


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("window", [None, "minimal-window"])
def test_observation_never_restarts(monkeypatch, trusted, window):
    session = local_mac.MacSession.__new__(local_mac.MacSession)
    session.registry, session._keymap, session._ref_keys = {}, {}, {}
    identity = {"pid": 42, "name": "Fake"}
    monkeypatch.setattr(local_mac, "_resolve_app", lambda _: identity)
    monkeypatch.setattr(local_mac, "AXUIElementCreateApplication", lambda _: "app")
    monkeypatch.setattr(local_mac, "AXIsProcessTrusted", lambda: trusted)
    monkeypatch.setattr(local_mac, "_embedded_chromium", lambda _: True)
    monkeypatch.setattr(local_mac, "_enable_manual_ax", lambda _: None)
    monkeypatch.setattr(local_mac.ax, "get_window", lambda _: window)
    monkeypatch.setattr(local_mac, "launch_app", lambda *a, **k: pytest.fail("observation relaunched"))
    win, _, error = session._resolve_window("Fake")
    assert win == window
    if error:
        assert "blocks AX entirely" not in error[0]
        if not trusted:
            assert "NOT trusted" in error[0]


def test_launch_failure_does_not_claim_success(monkeypatch):
    monkeypatch.setattr(local_mac, "_resolve_app", lambda _: None)
    monkeypatch.setattr(local_mac.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="missing"))
    assert local_mac.launch_app("Missing", background=True) == "failed to launch Missing: missing"


def test_sdk_uses_fresh_foreground(monkeypatch):
    monkeypatch.setattr(local_mac, "_frontmost", lambda: ("Fresh", 7))
    assert sdk._frontmost() == "Fresh"


def test_native_refs_expire_when_process_identity_changes(monkeypatch):
    session = local_mac.MacSession.__new__(local_mac.MacSession)
    session._identity = {"pid": 7, "started_at": "first"}
    session.registry, session._keymap, session._ref_keys = {"e1": object()}, {"a": "e1"}, {"e1": "a"}
    monkeypatch.setattr(local_mac, "_running_identity", lambda _: {"pid": 7, "started_at": "second"})
    with pytest.raises(local_mac.StaleRef):
        session._el("e1")
    assert not session.registry
