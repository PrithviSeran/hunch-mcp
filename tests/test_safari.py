import json
import os
from pathlib import Path
import plistlib
import shutil
import stat

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from hunch.gate import HunchError, WebNotOpen
from hunch.safari import (PROTOCOL_VERSION, SafariBridgeClient, SafariComputer,
                          ensure_bridge_token, install_bundled_companion)


def test_bridge_token_is_stable_and_owner_only(tmp_path):
    path = tmp_path / "state" / "token"
    first = ensure_bridge_token(path)
    second = ensure_bridge_token(path)
    assert first == second and len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_client_authenticates_and_validates_receipt(tmp_path):
    seen = []

    def transport(request):
        seen.append(request)
        return {"protocol": PROTOCOL_VERSION, "id": request["id"], "status": "verified"}

    client = SafariBridgeClient(token_path=tmp_path / "token", transport=transport)
    assert client.request("status")["status"] == "verified"
    assert seen[0]["operation"] == "status"
    assert seen[0]["token"] == (tmp_path / "token").read_text().strip()
    schema_path = Path(__file__).resolve().parents[1] / "schemas/safari-bridge-v1.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text()), format_checker=FormatChecker())
    validator.validate(seen[0])
    client.request("open", {"url": "https://example.com", "newWindow": True})
    validator.validate(seen[-1])
    client.request("act", {
        "tabId": 41, "windowId": 8, "windowLease": "lease-1",
        "expectedUrl": "https://example.com", "expectedOrigin": "https://example.com",
        "generation": "doc-1", "captureScreenshot": True,
    })
    validator.validate(seen[-1])

    bad = SafariBridgeClient(token_path=tmp_path / "other",
                             transport=lambda request: {"protocol": 99, "id": request["id"]})
    with pytest.raises(HunchError, match="protocol version"):
        bad.request("status")
    with pytest.raises(HunchError, match="unsupported Safari bridge operation"):
        client.request("evaluate_javascript", {"source": "document.body.innerHTML"})


def test_install_is_idempotent_and_updates_atomically(tmp_path):
    source = tmp_path / "source" / "Hunch Safari.app"
    marker = source / "Contents" / "Info.plist"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"version":"1"}')
    (source / "Contents" / "MacOS").mkdir()
    (source / "Contents" / "MacOS" / "Hunch Safari").write_text("binary-v1")

    destination = tmp_path / "installed"
    result = install_bundled_companion(destination=destination, bundled=source)
    assert result.state == "installed"
    assert Path(result.path, "Contents/MacOS/Hunch Safari").read_text() == "binary-v1"
    assert install_bundled_companion(destination=destination, bundled=source).state == "ready"

    marker.write_text('{"version":"2"}')
    (source / "Contents" / "MacOS" / "Hunch Safari").write_text("binary-v2")
    assert install_bundled_companion(destination=destination, bundled=source).state == "installed"
    assert Path(result.path, "Contents/MacOS/Hunch Safari").read_text() == "binary-v2"
    assert not Path(str(result.path) + ".previous").exists()


def test_resolve_install_dir_prefers_existing_system_app_then_home(tmp_path):
    from hunch.safari import COMPANION_BUNDLE, resolve_install_dir
    system = tmp_path / "sys"
    home = tmp_path / "home"
    (system / COMPANION_BUNDLE).mkdir(parents=True)
    assert resolve_install_dir(system, home, access=lambda *args: False) == system
    shutil.rmtree(system / COMPANION_BUNDLE)
    assert resolve_install_dir(system, home, access=lambda *args: False) == home
    assert home.is_dir()


def test_onboarding_prompts_once_when_extension_is_disabled(tmp_path):
    from hunch.safari import offer_safari_onboarding
    seen = []
    notes = []

    class DisabledBridge:
        def request(self, operation, payload=None):
            seen.append(operation)
            return {"status": "blocked", "reason": "disabled"}

    marker = tmp_path / "offered"
    assert offer_safari_onboarding(
        client=DisabledBridge(), notifier=lambda msg, title="Hunch": notes.append((title, msg)),
        marker=marker,
    ) == "offered"
    assert notes and marker.exists()
    assert seen == ["status", "tabs"]
    assert offer_safari_onboarding(
        client=DisabledBridge(), notifier=lambda msg, title="Hunch": notes.append((title, msg)),
        marker=marker,
    ) == "pending"
    assert len(notes) == 1


def test_onboarding_is_silent_when_extension_is_ready(tmp_path):
    from hunch.safari import offer_safari_onboarding
    notes = []

    class ReadyBridge:
        def request(self, operation, payload=None):
            return {"status": "verified", "extensionEnabled": True}

    assert offer_safari_onboarding(
        client=ReadyBridge(), notifier=lambda *args, **kwargs: notes.append(1),
        marker=tmp_path / "offered",
    ) == "ready"
    assert notes == []


class FakeBridge:
    def __init__(self):
        self.calls = []

    def request(self, operation, payload=None):
        self.calls.append((operation, payload or {}))
        if operation == "status":
            return {"status": "verified", "extensionEnabled": True}
        if operation == "open":
            return {"protocol": 1, "id": "ignored", "status": "verified", "tabId": 41,
                    "windowId": 8, "windowLease": "lease-1", "ownedWindow": True,
                    "url": "https://example.com/form", "generation": "doc-1", "elementCount": 2}
        if operation == "snapshot":
            return {"status": "verified", "tabId": 41, "url": "https://example.com/form",
                    "generation": "doc-1", "tree": "[e1] textbox name=\"Name\""}
        if operation == "act" and payload.get("captureScreenshot"):
            return {"status": "verified", "tabId": 41, "windowId": 8,
                    "windowLease": "lease-1", "ownedWindow": True, "url": "https://example.com/form",
                    "generation": "doc-1", "mimeType": "image/png", "data": "aGVsbG8="}
        if operation == "act":
            return {"status": "verified", "tabId": 41, "windowId": 8,
                    "windowLease": "lease-1", "ownedWindow": True, "url": "https://example.com/form",
                    "generation": "doc-1", "tree": "[e1] textbox filled=true",
                    "receipts": [{"status": "verified"}]}
        if operation == "tabs":
            return {"status": "verified", "tabs": [{"index": 0, "tabId": 41,
                    "title": "Form", "url": "https://example.com/form", "current": True}]}
        raise AssertionError(operation)


def test_safari_computer_pins_every_mutation_to_tab_url_origin_and_generation(monkeypatch):
    bridge = FakeBridge()
    computer = SafariComputer(client=bridge, allowed_origins=("https://example.com",))
    monkeypatch.setattr("hunch.safari.prepare_bundled_companion",
                        lambda: type("Install", (), {"state": "ready"})())
    assert "background window" in computer.open("https://example.com/form", new_window=True)
    assert bridge.calls[-1] == ("open", {"url": "https://example.com/form", "newWindow": True})
    assert computer.snapshot().startswith("[e1]")
    assert "filled=true" in computer.act([{"action": "type", "ref": "e1", "text": "Ada"}])
    operation, payload = bridge.calls[-1]
    assert operation == "act"
    assert payload["tabId"] == 41
    assert payload["windowId"] == 8
    assert payload["windowLease"] == "lease-1"
    assert payload["expectedUrl"] == "https://example.com/form"
    assert payload["expectedOrigin"] == "https://example.com"
    assert payload["generation"] == "doc-1"
    assert computer.capture_screenshot() == "aGVsbG8="


def test_open_prompts_when_safari_extension_is_not_enabled(tmp_path, monkeypatch):
    from hunch.safari import SafariComputer, offer_safari_onboarding
    notes = []

    class Disabled:
        def request(self, operation, payload=None):
            if operation == "status":
                return {"status": "blocked", "reason": "disabled"}
            if operation == "tabs":
                return {"status": "blocked", "reason": "disabled"}
            if operation == "open":
                return {"status": "blocked", "reason": "Enable Hunch in Safari Settings"}
            raise AssertionError(operation)

    monkeypatch.setattr("hunch.safari.prepare_bundled_companion",
                        lambda: type("Install", (), {"state": "installed"})())
    monkeypatch.setattr(
        "hunch.safari.offer_safari_onboarding",
        lambda **kwargs: offer_safari_onboarding(
            client=kwargs.get("client") or Disabled(),
            notifier=lambda msg, title="Hunch": notes.append(msg),
            marker=tmp_path / "offered",
        ),
    )
    result = SafariComputer(client=Disabled(), allowed_origins=("https://example.com",)).open(
        "https://example.com/form")
    assert result.startswith("BLOCKED")
    assert notes
    assert (tmp_path / "offered").exists()


def test_socket_failure_has_single_actionable_onboarding_message(tmp_path):
    client = SafariBridgeClient(endpoint_path=tmp_path / "missing.json",
                                token_path=tmp_path / "token", timeout=0.01)
    with pytest.raises(WebNotOpen) as exc:
        client.request("status")
    message = str(exc.value)
    assert "Safari Settings > Extensions" in message
    assert "website access" in message


def test_sdk_routes_safari_without_changing_the_public_web_tools(monkeypatch):
    from hunch.sdk import Hunch

    class DummySafari:
        backend = "safari"
        app = "Safari"
        port = None
        editor = False

        def __init__(self, allowed_origins=()):
            self.allowed_origins = allowed_origins
            self.session = self
            self.target_id = 2

        def open(self, url, new_window=False):
            return f"safari:{url}"

        def close(self):
            pass

    monkeypatch.setattr("hunch.safari.SafariComputer", DummySafari)
    hunch = Hunch(check_permissions=False, confirm="off")
    assert hunch.web.open("https://example.com/form", app="Apple Safari") == \
        "safari:https://example.com/form"
    assert hunch.web._computer.backend == "safari"
    assert hunch.web.open("https://example.com/form", app="Safari", isolated=True).startswith("REFUSED")


def test_release_wheel_source_includes_current_notarized_companion():
    from hunch.safari import _bundled_companion
    bundled = _bundled_companion()
    assert bundled is not None
    with bundled.joinpath("Contents", "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == "com.tryhunch.safari"
    assert info["CFBundleVersion"] == "7"

    root = Path(__file__).resolve().parents[1]
    packaged_resources = bundled / "Contents/PlugIns/Hunch.appex/Contents/Resources"
    source_resources = root / "native/safari/Extension/Resources"
    for name in ("background.js", "content.js", "manifest.json"):
        assert packaged_resources.joinpath(name).read_bytes() == source_resources.joinpath(name).read_bytes()
