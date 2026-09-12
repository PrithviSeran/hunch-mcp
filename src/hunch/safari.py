"""Focus-free Safari Web Extension transport.

Safari does not expose CDP.  Hunch therefore talks to a signed containing app over an
authenticated loopback connection; the app dispatches typed messages to the Safari Web Extension.
The extension is the only component that touches page DOM.  There is deliberately no raw
JavaScript command in this protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import uuid

from .gate import HunchError, WebNotOpen


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
COMPANION_BUNDLE = "Hunch Safari.app"
DEFAULT_STATE_DIR = Path(os.path.expanduser(
    "~/Library/Containers/com.tryhunch.safari/Data/Library/Application Support/Hunch"
))
DEFAULT_ENDPOINT = DEFAULT_STATE_DIR / "endpoint.json"
DEFAULT_TOKEN = DEFAULT_STATE_DIR / "bridge-token"
ONBOARDING_MARKER = Path(os.path.expanduser(
    "~/Library/Application Support/Hunch/safari-onboarding-offered"
))


@dataclass(frozen=True)
class CompanionInstall:
    state: str
    path: str = ""
    detail: str = ""


def _bundled_companion():
    """Return a Traversable for the release-bundled app, or None in source checkouts."""
    candidate = resources.files("hunch").joinpath("native", COMPANION_BUNDLE)
    try:
        return candidate if candidate.is_dir() else None
    except (FileNotFoundError, OSError):
        return None


def default_install_dir():
    return resolve_install_dir()


def resolve_install_dir(system_dir=None, home_dir=None, *, access=os.access):
    """Put the containing app where Safari will actually list its extension.

    PluginKit/Safari ignore helpers hidden under Application Support. Prefer an existing
    ``/Applications`` copy, then a writable ``/Applications``, then ``~/Applications``.
    """
    system = Path(system_dir or "/Applications")
    if (system / COMPANION_BUNDLE).is_dir():
        return system
    if access(str(system), os.W_OK):
        return system
    home = Path(home_dir or (Path.home() / "Applications"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def install_bundled_companion(*, destination=None, bundled=None):
    """Atomically stage the companion bundled in the wheel.

    This is safe to call on every ``hunch serve``.  Missing assets are expected in editable
    source installations, while release wheels are required to contain the signed app.
    Registration/permission is intentionally not faked here: Safari owns those decisions.
    """
    source = bundled if bundled is not None else _bundled_companion()
    if source is None:
        return CompanionInstall("unavailable", detail="this installation has no bundled Safari companion")
    destination = Path(destination or default_install_dir()) / COMPANION_BUNDLE
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Info.plist carries both marketing version and build number, so it is a stable update
    # marker without trusting timestamps from wheel extraction.
    marker = "Contents/Info.plist"
    try:
        source_marker = source.joinpath(*marker.split("/")).read_text()
    except (FileNotFoundError, OSError):
        source_marker = ""
    try:
        installed_marker = (destination / marker).read_text()
    except (FileNotFoundError, OSError):
        installed_marker = None
    if destination.is_dir() and source_marker and source_marker == installed_marker:
        return CompanionInstall("ready", str(destination), "already current")

    staging = Path(tempfile.mkdtemp(prefix=".hunch-safari-", dir=destination.parent)) / COMPANION_BUNDLE
    try:
        # importlib Traversable objects from wheels are materialized by as_file.
        with resources.as_file(source) as source_path:
            shutil.copytree(source_path, staging, symlinks=True)
        if destination.exists():
            backup = destination.with_name(destination.name + ".previous")
            if backup.exists():
                shutil.rmtree(backup)
            destination.rename(backup)
            staging.rename(destination)
            shutil.rmtree(backup)
        else:
            staging.rename(destination)
    finally:
        root = staging.parent
        if root.exists():
            shutil.rmtree(root)
    return CompanionInstall("installed", str(destination), "staged from the hunch-sdk wheel")


def prepare_bundled_companion(*, destination=None, bundled=None, launcher=None):
    """Stage and quietly start the containing app so Safari can register its extension."""
    result = install_bundled_companion(destination=destination, bundled=bundled)
    if result.path:
        launch = launcher or (lambda path: subprocess.run(
            ["/usr/bin/open", "-gj", path], check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10))
        try:
            launch(result.path)
        except (OSError, subprocess.TimeoutExpired):
            return CompanionInstall("installed_not_running", result.path,
                                    "staged, but the containing app did not start")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if DEFAULT_ENDPOINT.exists():
                break
            time.sleep(0.05)
    return result


def offer_safari_onboarding(*, client=None, notifier=None, marker=ONBOARDING_MARKER):
    """Prompt once to enable the Safari extension after the companion is staged.

    Apple still owns the two clicks (enable + website access). Hunch can only open the
    exact Safari Settings pane and tell the user why. Repeating that on every MCP launch
    would steal focus, so a marker makes the prompt one-time until the user deletes it.
    """
    from .notify import notify as desktop_notify
    notify_user = notifier or desktop_notify
    bridge = client or SafariBridgeClient(timeout=2.0)
    message = (
        "Enable Hunch in Safari → Settings → Extensions, then allow website access. "
        "Look for “Hunch”, not “Hunch Safari”."
    )
    try:
        response = bridge.request("status")
    except (HunchError, WebNotOpen, OSError):
        response = {}
    if response.get("status") == "verified" and response.get("extensionEnabled", True):
        return "ready"
    if Path(marker).exists():
        return "pending"
    notify_user(message, title="Hunch Safari")
    try:
        # Non-status operations open Safari's extension settings when Hunch is disabled.
        bridge.request("tabs")
    except (HunchError, WebNotOpen, OSError):
        pass
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("offered\n")
    return "offered"


def ensure_bridge_token(path=DEFAULT_TOKEN):
    """Create the per-user bridge secret once, with owner-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text().strip()
        if token:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text().strip()
        if token:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            return token
        raise
    try:
        os.write(fd, (token + "\n").encode())
    finally:
        os.close(fd)
    return token


class SafariBridgeClient:
    """One-request-per-connection JSON transport to the signed companion."""

    def __init__(self, endpoint_path=DEFAULT_ENDPOINT, token_path=DEFAULT_TOKEN,
                 timeout=15.0, transport=None):
        self.endpoint_path = Path(endpoint_path)
        self.token_path = Path(token_path)
        self.timeout = timeout
        self._transport = transport or self._socket_request

    def request(self, operation, payload=None):
        if operation not in {"status", "open", "tabs", "switch_tab", "snapshot", "act"}:
            raise HunchError(f"unsupported Safari bridge operation: {operation}")
        if payload is not None and not isinstance(payload, dict):
            raise HunchError("Safari bridge payload must be an object")
        request = {
            "protocol": PROTOCOL_VERSION,
            "id": str(uuid.uuid4()),
            "token": ensure_bridge_token(self.token_path),
            "operation": operation,
            "payload": payload or {},
        }
        if len(json.dumps(request).encode()) > MAX_REQUEST_BYTES:
            raise HunchError("Safari bridge request exceeded 4 MiB")
        response = self._transport(request)
        if not isinstance(response, dict):
            raise HunchError("Safari companion returned a malformed response")
        if response.get("id") != request["id"]:
            raise HunchError("Safari companion response did not match the request")
        if response.get("protocol") != PROTOCOL_VERSION:
            raise HunchError("Safari companion protocol version does not match hunch-sdk")
        return response

    def _socket_request(self, request):
        encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        deadline = time.monotonic() + self.timeout
        last_error = None
        try:
            while True:
                try:
                    endpoint_stat = self.endpoint_path.stat()
                    if endpoint_stat.st_uid != os.getuid() or endpoint_stat.st_mode & 0o022:
                        raise HunchError("Safari companion endpoint metadata is not owner-controlled")
                    endpoint = json.loads(self.endpoint_path.read_text())
                    port = int(endpoint["port"])
                    if not 1024 <= port <= 65535:
                        raise ValueError("invalid port")
                    remaining = max(0.05, deadline - time.monotonic())
                    with socket.create_connection(("127.0.0.1", port), timeout=remaining) as connection:
                        connection.sendall(encoded)
                        chunks = bytearray()
                        while b"\n" not in chunks:
                            part = connection.recv(65536)
                            if not part:
                                break
                            chunks.extend(part)
                            if len(chunks) > MAX_RESPONSE_BYTES:
                                raise HunchError("Safari companion response exceeded 16 MiB")
                    break
                except HunchError:
                    raise
                except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError,
                        ConnectionRefusedError, socket.timeout, OSError) as exc:
                    last_error = exc
                    if time.monotonic() >= deadline:
                        raise exc
                    time.sleep(0.05)
        except HunchError:
            raise
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError,
                ConnectionRefusedError, socket.timeout, OSError) as exc:
            if isinstance(exc, socket.timeout):
                raise WebNotOpen(
                    "Safari bridge timed out waiting for a response. Extension enablement "
                    "and website permission are unknown; this is not evidence they are disabled. "
                    "Inspect web_tabs before retrying an action, since it may have completed."
                ) from exc
            raise WebNotOpen(
                f"Safari companion transport failed ({type(exc).__name__}: {exc}). "
                "This does not establish that the extension is disabled. Check the companion "
                "connection; native Safari tools remain available under the session policy."
            ) from (last_error or exc)
        try:
            return json.loads(bytes(chunks).split(b"\n", 1)[0])
        except (ValueError, UnicodeDecodeError) as exc:
            raise HunchError("Safari companion returned invalid JSON") from exc


class SafariComputer:
    """Adapter matching the subset of ``CDPComputer`` used by the public web tools."""

    backend = "safari"
    app = "Safari"
    port = None
    editor = False

    def __init__(self, client=None, allowed_origins=()):
        self.client = client or SafariBridgeClient()
        self.allowed_origins = tuple(allowed_origins)
        self.session = self
        self.target_id = None
        self.window_id = None
        self.window_lease = ""
        self.owned_window = False
        self._url = ""
        self._generation = ""

    @staticmethod
    def _reason(response):
        return response.get("reason") or response.get("detail") or "unknown Safari bridge error"

    def _call(self, operation, payload=None):
        response = self.client.request(operation, payload)
        status = response.get("status")
        if status in {"blocked", "refused", "failed"}:
            return response
        if status not in {"verified", "performed_unverified"}:
            raise HunchError("Safari companion returned an unknown receipt status")
        return response

    def open(self, url, new_window=False):
        from .destinations import navigation_refusal
        refusal = navigation_refusal(url, self.allowed_origins) if url else ""
        if refusal:
            return refusal
        install = prepare_bundled_companion()
        if install.state != "unavailable":
            offer_safari_onboarding(client=self.client)
        response = self._call("open", {"url": url, "newWindow": bool(new_window)})
        if response.get("status") != "verified":
            return f"{response.get('status', 'blocked').upper()}: {self._reason(response)}"
        self._bind(response)
        installed = "" if install.state == "ready" else f"; companion {install.state}"
        surface = "background window" if response.get("ownedWindow") else "selected tab" if not url else "tab"
        return f"opened Safari {surface} focus-free ({response.get('elementCount', 0)} elements{installed})"

    def _bind(self, response):
        self.target_id = response.get("tabId")
        self.window_id = response.get("windowId", self.window_id)
        self.window_lease = response.get("windowLease", self.window_lease)
        self.owned_window = response.get("ownedWindow", self.owned_window)
        self._url = response.get("url", self._url)
        self._generation = response.get("generation", self._generation)

    def _binding(self):
        if self.target_id is None or not self._url or not self._generation:
            raise WebNotOpen("no bound Safari tab — call web_open(app='Safari', url=...) first")
        return {"tabId": self.target_id, "windowId": self.window_id,
                "windowLease": self.window_lease, "expectedUrl": self._url,
                "expectedOrigin": _origin(self._url), "generation": self._generation}

    def snapshot(self):
        response = self._call("snapshot", self._binding())
        if response.get("status") != "verified":
            return f"{response.get('status', 'blocked').upper()}: {self._reason(response)}"
        self._bind(response)
        return response.get("tree", "")

    def act(self, actions, detailed=False, postcondition=None):
        if len(actions) > 50:
            return "REFUSED: Safari actions are limited to 50 per call"
        supported = {"click", "check", "type", "navigate", "click_xy", "drag", "key"}
        unknown = [action.get("action") for action in actions if action.get("action") not in supported]
        if unknown:
            return "UNSUPPORTED: Safari MCP beta does not support action " + repr(unknown[0])
        if any(action.get("action") == "navigate" for action in actions) and len(actions) != 1:
            return "REFUSED: Safari navigation must be the only action in its call"
        payload = {**self._binding(), "actions": actions, "detailed": bool(detailed)}
        if postcondition is not None:
            payload["postcondition"] = postcondition
        response = self._call("act", payload)
        if response.get("status") not in {"verified", "performed_unverified"}:
            return response if detailed else f"{response.get('status', 'failed').upper()}: {self._reason(response)}"
        self._bind(response)
        if detailed:
            return response
        prefix = "PERFORMED_UNVERIFIED:\n" if response.get("status") == "performed_unverified" else ""
        return prefix + response.get("tree", response.get("summary", "verified"))

    def capture_screenshot(self):
        response = self._call("act", {**self._binding(), "captureScreenshot": True})
        if response.get("status") != "verified":
            raise HunchError(f"{response.get('status', 'failed').upper()}: {self._reason(response)}")
        self._bind(response)
        data = response.get("data", "")
        if not data:
            raise HunchError("Safari companion returned an empty screenshot")
        return data

    def tabs(self):
        response = self._call("tabs")
        return response.get("tabs", []) if response.get("status") == "verified" else response

    def switch_tab(self, index):
        response = self._call("switch_tab", {"index": index, "windowId": self.window_id,
                                               "windowLease": self.window_lease})
        if response.get("status") != "verified":
            return f"{response.get('status', 'blocked').upper()}: {self._reason(response)}"
        self._bind(response)
        return f"switched to Safari tab {index}; call web_snapshot to read it"

    def url(self):
        return self._url

    def close(self):
        # The user's Safari and extension stay running; only local binding is discarded.
        pass


def _origin(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port
    suffix = "" if port is None or (parsed.scheme == "https" and port == 443) \
        or (parsed.scheme == "http" and port == 80) else f":{port}"
    return f"{parsed.scheme.lower()}://{host.lower()}{suffix}"


def is_safari(app):
    return str(app or "").strip().lower() in {"safari", "apple safari"}
