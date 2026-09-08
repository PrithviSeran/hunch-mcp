"""Hunch CDP backend — drive Chromium browsers and Electron apps FOCUS-FREE.

Supported Chromium/Electron launches expose the Chrome DevTools Protocol.
A verified endpoint provides renderer accessibility semantics, DOM references,
and renderer-local input without moving the OS cursor. Native AX can also work
in the background; coverage depends on the operation and current app state.

Same snapshot/act/handle contract as local_mac.LocalComputer, so it drops into
the same agent loop.
"""

import os
import re
import json
import base64
import struct
import zlib
import time
import socket
import subprocess
import tempfile
import urllib.request
import urllib.parse
import ipaddress

import websocket  # websocket-client


def _host_resolves(host):
    try:
        socket.getaddrinfo(host, None)
        return True
    except Exception:
        return False


def _is_blocked_host(host):
    """True for loopback / private / link-local / reserved / multicast hosts (SSRF)."""
    if not host:
        return False
    h = host.lower().strip()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    raw = h.strip("[]")  # [::1] -> ::1
    try:
        ip = ipaddress.ip_address(raw)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified or ip.is_multicast)
    except ValueError:
        pass
    # hostname -> resolve and check any A/AAAA is private
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        for _, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if (ip.is_private or ip.is_loopback or ip.is_link_local
                        or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
                    return True
            except ValueError:
                continue
    except Exception:
        return False  # NXDOMAIN handled by caller
    return False


# ── ARIA/CDP roles worth surfacing (interactive + content), like the AX filter ──
_INTERACTIVE = {"link", "button", "textbox", "searchbox", "checkbox", "radio",
                "combobox", "listbox", "option", "menuitem", "menuitemcheckbox",
                "menuitemradio", "tab", "switch", "slider", "spinbutton", "textfield"}
_CONTENT = {"heading", "statictext", "cell", "gridcell", "listitem", "paragraph",
            "rowheader", "columnheader", "caption"}
_ROW_ROLES = {"row", "treeitem", "listitem"}
# Modals/toasts — always surface so an agent never misses a confirmation dialog that's
# blocking its action (e.g. Gmail's "Confirm bulk action" alertdialog behind a delete).
_CONTAINER = {"dialog", "alertdialog", "alert"}


# Persistent, DEDICATED Hunch profile: the user logs into it once, and CDP reuses it
# forever. It is a NON-default --user-data-dir, so Chrome 136+ allows the debug port on
# it (unlike the real/default profile, where the port is silently refused).
from .cli import CHROME_PROFILE as HUNCH_PROFILE  # single home for the path (see cli.py)

# How an agent might name a Chromium browser -> the exact macOS app name `open` needs.
# Agents frequently pass "Chrome" (real name is "Google Chrome") or even a website like
# "Gmail" — resolve both so the launch doesn't silently fail with "app not found".
_BROWSER_ALIASES = {
    "": "Google Chrome", "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "googlechrome": "Google Chrome", "chromium": "Chromium", "arc": "Arc",
    "brave": "Brave Browser", "brave browser": "Brave Browser",
    "edge": "Microsoft Edge", "microsoft edge": "Microsoft Edge",
    "vivaldi": "Vivaldi", "opera": "Opera",
    # website / product names passed by mistake -> default browser
    "gmail": "Google Chrome", "google": "Google Chrome", "mail": "Google Chrome",
    "google mail": "Google Chrome", "browser": "Google Chrome",
}


def _resolve_app(app_name):
    """Map a loose browser name to the exact installed app name."""
    return _BROWSER_ALIASES.get((app_name or "").strip().lower(), app_name)


# ── Electron code editors — drivable over CDP exactly like a browser ──────────────────────
# Their integrated terminal is xterm.js, which AX can READ but never WRITE (the AX value-set
# lands in a screen-reader mirror, not the PTY). Over CDP we inject REAL keystrokes into the
# renderer, so the terminal — and its running claude/shell — becomes focus-free-typeable.
_EDITOR_ALIASES = {
    "cursor": "Cursor",
    "code": "Visual Studio Code", "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code", "visual studio code": "Visual Studio Code",
    "vscodium": "VSCodium", "codium": "VSCodium",
    "windsurf": "Windsurf",
}


def _resolve_editor(app):
    """Map a loose editor name to the exact installed app name (unchanged if not an editor)."""
    return _EDITOR_ALIASES.get((app or "").strip().lower(), app)


def _is_editor(app):
    """True if `app` names an Electron code editor we drive over CDP (Cursor/VS Code/…)."""
    a = (app or "").strip().lower()
    return a in _EDITOR_ALIASES or _resolve_editor(app) in _EDITOR_ALIASES.values()


def editor_target(app):
    """(port, profile, real_app_name) for an editor. Each editor gets its OWN dedicated Hunch
    profile + a deterministic port, so driving it never collides with the Chrome CDP profile
    and never touches the user's OWN editor window — a separate, focus-free Hunch instance,
    the same non-destructive pattern Hunch uses for Chrome."""
    real = _resolve_editor(app)
    slug = re.sub(r"[^a-z0-9]+", "-", real.lower()).strip("-") or "editor"
    profile = os.path.expanduser(f"~/.hunch/{slug}-cdp")
    port = 9360 + (zlib.crc32(real.encode()) % 40)   # 9360-9399, off the browser port (9337)
    return port, profile, real


# Side-panel windows an editor exposes as extra page targets — bind the real editor, not these.
# Precise titles (not bare 'agents') so a workspace folder literally named "agents" isn't excluded.
_EDITOR_SIDE_PANELS = ("cursor agents",)

# VS Code-family window titles are em/en-dash separated: "<file> — <workspace>" (or bare
# "<workspace>" for an empty editor). Split on the dash ONLY, never a hyphen — plenty of real
# folders are named "my-project".
_TITLE_SEP = re.compile(r"\s+[—–]\s+")


def _title_segments(title):
    return [s.strip() for s in _TITLE_SEP.split((title or "").strip()) if s.strip()]


def _title_workspace(title):
    """The workspace segment of an editor window title ('a3.html — C63' -> 'C63'). The window's
    workspace is what identifies it — an editor instance can hold several windows on different
    folders, and they are indistinguishable by url (all workbench.html)."""
    segs = _title_segments(title)
    return segs[-1] if segs else ""


def _title_matches(title, path):
    """True if an editor window title names `path` — as its workspace (a folder) or as its open
    file. Basename comparison: the title never carries the full path."""
    base = os.path.basename(os.path.normpath(path or ""))
    if not base or base in (".", "/"):
        return False
    return base.casefold() in [s.casefold() for s in _title_segments(title)]


def _pick_workbench(pages, folder=None):
    """From an editor's page targets, choose the real editor window: a workbench.html target whose
    title is NOT a side panel (Cursor's 'Cursor Agents', etc.). Returns None if none qualify yet —
    the workbench target can lag the side panel at startup, so callers poll on this.
    Ambiguous windows require explicit target selection.

    With `folder`, a window whose title names that folder WINS. One editor instance commonly has
    several windows open (and restores the previous session's on launch), so 'the first workbench'
    is a coin flip — that is how web_open ended up driving a stale, unrelated workspace while
    reporting the requested one."""
    wb = [p for p in (pages or []) if "workbench.html" in (p.get("url", "") or "")]
    main = [p for p in wb if not any(s in (p.get("title", "") or "").lower() for s in _EDITOR_SIDE_PANELS)]
    if folder:
        hit = [p for p in main if _title_matches(p.get("title"), folder)]
        if hit:
            return hit[0] if len(hit) == 1 else None
    return main[0] if len(main) == 1 else None


def open_folder_in_editor(app, profile, folder, background=True):
    """Ask an ALREADY-RUNNING Hunch editor instance to open `folder` in a new window.

    Electron keeps ONE instance per --user-data-dir, so this second launch hands its argv to the
    running instance and exits, instead of starting a rival that would fight for the profile lock.
    This is the only way to change what a live instance has open: a folder passed at LAUNCH is seen
    only by a cold start — `launch_chromium` reuses a live instance and never replays it."""
    cmd = ["open"] + (["-g"] if background else []) + \
          ["-na", app, "--args", f"--user-data-dir={profile}", folder]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0


def _wait_for_port(port, timeout=15):
    """Poll the CDP HTTP endpoint until the debug port binds."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"debug port {port} did not open within {timeout}s")


_OWNED_ENDPOINTS = {}


def _endpoint_listeners(port):
    result = subprocess.run(["lsof", "-nP", "-FpdRn", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=3)
    listeners = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            current = listeners.setdefault(int(line[1:]), {"parent": None, "sockets": set()})
        elif current is not None and line.startswith("R"):
            current["parent"] = int(line[1:])
        elif current is not None and line.startswith("d"):
            current["sockets"].add(line[1:])
    if result.returncode:
        return {}
    return listeners


def _endpoint_owner(listeners):
    if len(listeners) == 1:
        return next(iter(listeners))
    # A forked helper can retain the parent's listening FD. Accept only the same
    # kernel socket and an unbroken ancestry chain among its holders; names and
    # matching ports alone cannot distinguish independent listeners.
    sockets = [entry["sockets"] for entry in listeners.values()]
    if not sockets or len(sockets[0]) != 1 or any(s != sockets[0] for s in sockets):
        return None
    socket_id = next(iter(sockets[0]))
    if not re.fullmatch(r"0x[0-9a-fA-F]+", socket_id) or int(socket_id, 16) == 0:
        return None
    roots = [pid for pid, entry in listeners.items() if entry["parent"] not in listeners]
    if len(roots) != 1:
        return None
    root = roots[0]
    for pid in listeners:
        seen = set()
        while pid != root:
            if pid in seen or pid not in listeners:
                return None
            seen.add(pid)
            pid = listeners[pid]["parent"]
    return root


def endpoint_identity(port):
    """Resolve the listening application, including verified inherited sockets."""
    from .local_mac import _running_identity
    from .targets import process_key
    listeners = _endpoint_listeners(port)
    pid = _endpoint_owner(listeners)
    if pid is None:
        raise RuntimeError(f"cannot verify one owning process for CDP port {port}")
    identity = _running_identity(pid)
    if not identity:
        raise RuntimeError(f"CDP port {port} owner has no application identity")
    if len(listeners) > 1:
        if (_endpoint_listeners(port) != listeners
                or process_key(_running_identity(pid) or {}) != process_key(identity)):
            raise RuntimeError(f"CDP port {port} ownership changed during verification; retry attachment")
    return identity


def _app_path(app):
    from AppKit import NSWorkspace
    if os.path.isabs(app):
        return os.path.realpath(app)
    path = NSWorkspace.sharedWorkspace().fullPathForApplication_(app)
    if not path:
        raise RuntimeError(f"cannot resolve installed application {app!r}")
    return os.path.realpath(str(path))


def verify_endpoint(port, app):
    identity = endpoint_identity(port)
    if os.path.realpath(identity.get("path", "")) != _app_path(_resolve_app(app)):
        raise RuntimeError(f"CDP port {port} belongs to {identity.get('path')}, not {app}")
    return identity


def launch_chromium(app_name, port, url=None, background=True, isolated=False, profile=None,
                    editor=False, allowed_origins=(), owner=None):
    """Launch a dedicated instance; reuse only a verified, compatible owned endpoint."""
    from .targets import process_key
    if url and not editor:
        from .destinations import navigation_refusal
        refusal = navigation_refusal(url, allowed_origins)
        if refusal:
            raise RuntimeError(refusal)
    app = _resolve_app(app_name)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
        listening = True
    except Exception:
        listening = False
    if listening:
        identity = verify_endpoint(port, app)
        owned = _OWNED_ENDPOINTS.get(port)
        if (not owned or owned.get("owner") is not owner or process_key(identity) != process_key(owned["identity"])
                or isolated or owned["profile"] != os.path.realpath(profile or HUNCH_PROFILE)):
            raise RuntimeError(f"CDP port {port} is already in use; explicitly attach or select a free port")
        return f"reusing verified CDP instance on :{port}"
    data_dir = tempfile.mkdtemp(prefix="hunch_cdp_", dir="/private/tmp") if isolated else os.path.realpath(profile or HUNCH_PROFILE)
    # Leave room for the editor's versioned socket basename (macOS limit: 103 bytes).
    if editor and len(os.fsencode(data_dir)) > 70:
        raise RuntimeError("editor profile path is too long for macOS IPC; choose a shorter cdp_profile")
    os.makedirs(data_dir, exist_ok=True)
    args = [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={data_dir}", "--no-first-run", "--no-default-browser-check",
            "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding"]
    if url:
        args.append(url)
    command = ["open"] + (["-g"] if background else []) + ["-na", app, "--args"] + args
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"failed to launch {app}: {result.stderr.strip()}")
    _wait_for_port(port, timeout=15)
    identity = verify_endpoint(port, app)
    from .local_mac import _proc_cmdline
    command_line = _proc_cmdline(identity["pid"])
    if f"--user-data-dir={data_dir}" not in command_line:
        raise RuntimeError("launched endpoint did not preserve the requested profile; ownership unverified")
    _OWNED_ENDPOINTS[port] = {"identity": identity, "profile": data_dir, "app": app,
                              "isolated": isolated, "owner": owner}
    return f"launched verified {app} on :{port} (profile={data_dir})"


def quit_cdp(port, *, owner=None):
    """Gracefully quit a process this runtime launched, after rechecking ownership."""
    from AppKit import NSRunningApplication
    from .targets import process_key
    from .local_mac import _process_alive
    owned = _OWNED_ENDPOINTS.get(port)
    if not owned or owned.get("owner") is not owner:
        raise RuntimeError(f"cannot quit unowned CDP endpoint :{port}; attach or inspect it first")
    identity = endpoint_identity(port)
    if process_key(identity) != process_key(owned["identity"]):
        raise RuntimeError("CDP ownership changed; refusing to quit")
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(identity["pid"])
    if app is None:
        raise RuntimeError("CDP owning process disappeared")
    app.terminate()
    deadline = time.monotonic() + 6
    while _process_alive(identity["pid"]) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _process_alive(identity["pid"]):
        raise RuntimeError("CDP application did not quit gracefully; resolve its save/confirmation prompt")
    del _OWNED_ENDPOINTS[port]
    return f"quit CDP instance on :{port}"


class CDPSession:
    """A CDP connection to one page/window, with a per-snapshot ref registry
    (ref -> backendDOMNodeId) so the agent acts on elements by ref."""

    def __init__(self, port):
        self.port = port
        self.ws = None
        self._id = 0
        self.registry = {}      # ref -> backendDOMNodeId
        self._counter = 0
        self.snapshot_count = 0
        self.target_id = None       # the page target this session's ws is bound to
        self._known_targets = set()  # target ids we've already seen (to detect NEW tabs)
        self.editor = False         # driving an Electron editor (multi-window) vs a browser
        self.pinned = None          # editor: the folder this session is deliberately bound to
        self.pinned_app = False
        self.allowed_origins = ()

    def _list_page_targets(self):
        """All page (tab/window) targets on this debug port. Chrome reports the most recently
        created/active page first."""
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=3))
        except Exception:
            return []
        return [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]

    def _open_ws(self, page):
        """(Re)bind this session's websocket to a specific page target and enable its CDP domains."""
        from urllib.parse import urlsplit
        address = urlsplit(page["webSocketDebuggerUrl"])
        if (address.scheme != "ws" or address.hostname not in ("127.0.0.1", "localhost", "::1")
                or address.port != self.port or address.username or address.password):
            raise RuntimeError("refusing CDP websocket outside the verified local endpoint")
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.registry.clear()
        self._observed_document = None
        self._last_screenshot_scale = None
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15,
                                              max_size=None, suppress_origin=True)
        self.target_id = page.get("id")
        self._id = 0
        for d in ("DOM", "Accessibility", "Runtime", "Page"):
            self._cmd(f"{d}.enable")
        # Background window, foreground behavior: make the page believe it's focused and force
        # its lifecycle active, so hasFocus()/focus-gated rendering and frozen pages behave as
        # if the tab were frontmost. Best-effort — some targets (Electron) reject these.
        for method, params in (("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                               ("Page.setWebLifecycleState", {"state": "active"})):
            try:
                self._cmd(method, params)
            except Exception:
                pass

    def connect(self, timeout=10, editor=False, folder=None):
        end = time.time() + timeout
        pages = []
        self.editor = bool(editor)
        while time.time() < end and not (pages if not editor else _pick_workbench(pages, folder)):
            pages = self._list_page_targets()
            if not pages or (editor and not _pick_workbench(pages, folder)):
                time.sleep(0.5)
        if not pages:
            raise RuntimeError(f"no CDP page target on :{self.port} — is the app launched with --remote-debugging-port?")
        # An editor exposes SEVERAL page targets that share the same workbench.html url — the real
        # editor window AND side panels like Cursor's "Agents" (title 'Cursor Agents'). They differ
        # only by title, so bind the editor window (has the tree + terminal), not a side panel —
        # and, when a folder was asked for, the window actually holding it.
        target = _pick_workbench(pages, folder) if editor else (pages[0] if len(pages) == 1 else None)
        if target is None:
            raise RuntimeError("multiple or unmatched CDP targets; explicitly select a target ID: "
                               + "; ".join(f"{p.get('id')}: {p.get('title')}" for p in pages))
        self._open_ws(target)
        self._known_targets = {p.get("id") for p in pages}
        return self

    def _follow_new_tab(self):
        """Follow the browser across tabs/windows. If a NEW tab opened since we last looked (a form
        or link that pops a new tab — exactly the case the agent couldn't reach before), move this
        session onto it. If our tab was closed, fall back to the newest remaining one. General
        multi-target handling; returns True if it switched."""
        pages = self._list_page_targets()
        if not pages:
            self.registry.clear()
            self._last_screenshot_scale = None
            raise RuntimeError("no CDP page targets available; inspect the endpoint again")
        ids = {p.get("id") for p in pages}
        if self.pinned_app:
            if self.target_id not in ids:
                self.registry.clear()
                raise RuntimeError("selected app renderer closed; explicitly select another target")
            self._known_targets = ids
            return False
        if self.editor:
            # An editor is MULTI-WINDOW: a new target is another window or a side panel the user
            # (or the editor itself) opened — never a page we navigated to. Auto-following one
            # would silently move the session off the workspace we were told to drive, which is
            # exactly how a read of "the hunch window" came back with someone else's project.
            # A replacement with the same title is still a different target.
            self._known_targets = ids
            if self.target_id in ids:
                return False
            self.registry.clear()
            self._last_screenshot_scale = None
            raise RuntimeError("selected editor window closed; explicitly select another target")
        new_pages = [p for p in pages if p.get("id") not in self._known_targets
                     and p.get("openerId") == self.target_id]
        switched = False
        if len(new_pages) == 1:
            self._open_ws(new_pages[0])       # newest-first -> the tab that just opened
            switched = True
        elif self.target_id not in ids:
            self.registry.clear()
            raise RuntimeError("selected CDP target closed; explicitly select another target")
        self._known_targets = ids
        return switched

    def workspace(self):
        """The workspace/folder this session's editor window is on (from its live title)."""
        return _title_workspace(self.title())

    def bind_workspace(self, folder, timeout=12):
        """Bind this session to the editor WINDOW that holds `folder`, polling until it appears.
        Returns True once bound, False if no such window exists within `timeout`.

        Editors are multi-window and every window shares the same workbench.html url, so a session
        must be pinned by TITLE. Callers use the False return to actually open the folder (see
        open_folder_in_editor) rather than reporting a workspace they never reached."""
        end = time.time() + timeout
        while True:
            pages = self._list_page_targets()
            hit = _pick_workbench(pages, folder)
            if hit is not None and _title_matches(hit.get("title"), folder):
                if hit.get("id") != self.target_id:
                    self._open_ws(hit)
                self._known_targets = {p.get("id") for p in pages}
                self.pinned = folder
                return True
            if time.time() >= end:
                return False
            time.sleep(0.5)

    def windows(self):
        """The editor's real windows (workbench targets), newest first, with their workspace."""
        return [{"index": i, "title": (p.get("title") or "")[:80],
                 "workspace": _title_workspace(p.get("title")),
                 "current": p.get("id") == self.target_id}
                for i, p in enumerate(self._list_page_targets())
                if "workbench.html" in (p.get("url", "") or "")]

    def tabs(self):
        pages = self._list_page_targets()
        return [{"index": i, "id": p.get("id"), "title": (p.get("title") or "")[:80], "url": (p.get("url") or "")[:120],
                 "current": p.get("id") == self.target_id} for i, p in enumerate(pages)]

    def switch_tab(self, index):
        pages = self._list_page_targets()
        if not pages:
            return "no open tabs"
        if index < 0 or index >= len(pages):
            return f"tab {index} out of range (0..{len(pages) - 1})"
        self._open_ws(pages[index])
        self._known_targets = {p.get("id") for p in pages}
        return f"switched to tab {index}: {(pages[index].get('title') or '')[:60]}"

    def _cmd(self, method, params=None, timeout=20):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            if m.get("method") == "Page.frameNavigated" and not m.get("params", {}).get("frame", {}).get("parentId"):
                self.registry.clear()
                self._last_screenshot_scale = None
        raise RuntimeError(f"{method}: timed out")

    def _ref(self, backend_id):
        self._counter += 1
        r = f"e{self._counter}"
        self.registry[r] = backend_id
        return r

    def _document_identity(self):
        frame = self._cmd("Page.getFrameTree").get("frameTree", {}).get("frame", {})
        return self.target_id, frame.get("id"), frame.get("loaderId"), frame.get("url")

    def validate_document(self):
        expected = getattr(self, "_observed_document", None)
        if expected is not None and self._document_identity() != expected:
            self.registry.clear()
            self._last_screenshot_scale = None
            raise RuntimeError("CDP document changed; take a fresh snapshot before acting")

    def url(self):
        try:
            return self._cmd("Runtime.evaluate", {"expression": "location.href", "returnByValue": True})["result"]["value"]
        except Exception:
            return ""

    def title(self):
        try:
            return self._cmd("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})["result"]["value"]
        except Exception:
            return ""

    # ── perception ──────────────────────────────────────────────────────
    def snapshot(self, compact=True, max_nodes=1500):
        self._follow_new_tab()   # if a click/form opened a new tab, move onto it before reading
        document = self._document_identity()
        self.registry = {}
        self.snapshot_count += 1
        nodes = self._cmd("Accessibility.getFullAXTree")["nodes"]
        by_id = {n["nodeId"]: n for n in nodes}
        roots = [n for n in nodes if not n.get("parentId")]
        lines = [f"=== {self.title()[:60]} — CDP snapshot #{self.snapshot_count} ==="]
        budget = {"left": max_nodes, "skipped": 0}
        for r in roots:
            self._walk(r, by_id, 0, lines, compact, budget)
        if budget["skipped"]:
            lines.append(f"…tree truncated at {max_nodes} elements shown (+{budget['skipped']} "
                         "more) — interact/scroll to change the page, then web_snapshot again")
        text = "\n".join(lines)
        if document != self._document_identity():
            self.registry.clear()
            raise RuntimeError("CDP document changed during observation; retry snapshot")
        self._observed_document = document
        return text, {"est_tokens": round(len(text) / 3.5), "refs": len(self.registry), "url": self.url()}

    @staticmethod
    def _name(n):
        v = (n.get("name", {}) or {}).get("value", "")
        return str(v) if v not in ("", None) else ""

    @staticmethod
    def _val(n):
        # AX values can be numeric (sliders, progress bars) — coerce so [:80] slicing works
        v = (n.get("value", {}) or {}).get("value", "")
        return str(v) if v not in ("", None) else ""

    def _interesting(self, role, name, val):
        rl = role.lower()
        if rl in _INTERACTIVE or rl in _CONTAINER:
            return True
        if rl in _CONTENT and (name or val):
            return True
        return False

    def _walk(self, n, by_id, depth, lines, compact, budget):
        role = (n.get("role", {}) or {}).get("value", "") or ""
        name = self._name(n)
        val = self._val(n)
        ignored = n.get("ignored")
        backend = n.get("backendDOMNodeId")

        emit = (not compact) or (not ignored and backend is not None and self._interesting(role, name, val))
        child_depth = depth
        # Budget caps EMITTED elements (output/token size), NOT nodes traversed. Counting
        # traversal instead would let a big page's ignored background (e.g. an aria-hidden
        # inbox behind a modal) exhaust the budget before we reach the real content/modal.
        if emit and budget["left"] <= 0:
            budget["skipped"] += 1   # counted so truncation is announced, never silent
            emit = False
        if emit:
            budget["left"] -= 1
            ref = self._ref(backend)
            parts = [f"[{ref}]", role]
            if name:
                parts.append(f'"{name[:90]}"')
            if val and val != name:
                parts.append(f"val={val[:80]!r}")
            props = {p["name"]: p.get("value", {}).get("value") for p in n.get("properties", [])}
            if props.get("disabled"):
                parts.append("disabled")
            lines.append("  " * depth + " ".join(parts))
            child_depth = depth + 1

        for cid in n.get("childIds", []):
            c = by_id.get(cid)
            if c is not None:
                self._walk(c, by_id, child_depth, lines, compact, budget)

    # ── actions (all focus-free: dispatched into the renderer) ───────────
    def _center(self, ref):
        backend = self.registry.get(ref)
        if backend is None:
            raise KeyError(f"stale ref {ref}")
        try:
            self._cmd("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend})
        except Exception:
            pass
        box = self._cmd("DOM.getBoxModel", {"backendNodeId": backend})["model"]["content"]
        return (box[0] + box[2]) / 2, (box[1] + box[5]) / 2

    def click(self, ref):
        # Native <select>/combobox: CDP mouse events can't open the OS dropdown — clicking
        # thrash (and getBoxModel on <option>) burns turns. Teach type-by-visible-text instead.
        backend = self.registry.get(ref)
        if backend is None:
            raise KeyError(f"stale ref {ref}")
        obj = self._cmd("DOM.resolveNode", {"backendNodeId": backend}).get("object", {}).get("objectId")
        if obj:
            kind = (self._cmd("Runtime.callFunctionOn",
                              {"objectId": obj, "functionDeclaration": self._SELECT_KIND_FN,
                               "returnByValue": True}).get("result", {}).get("value", ""))
            if kind == "IS_SELECT":
                return (f"REFUSED: {ref} is a native <select>/combobox — CDP cannot open the OS "
                        f"dropdown by clicking. Use web_act type on this same ref with the option's "
                        f"VISIBLE TEXT (e.g. type \"January\"), then web_snapshot to confirm.")
            if kind == "IS_OPTION":
                return (f"REFUSED: {ref} is a <select> option node — don't click options. Type the "
                        f"option's visible text into the parent <select>'s ref instead.")
        x, y = self._center(ref)
        for t in ("mousePressed", "mouseReleased"):
            self._cmd("Input.dispatchMouseEvent",
                      {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
        return f"clicked {ref}"

    def _image_to_viewport(self, x, y):
        """Map coordinates from the last web_screenshot PNG to CSS viewport coordinates.

        Page.captureScreenshot can return device-pixel images on Retina displays while CDP input
        events always consume CSS pixels.  Derive the scale from the actual PNG and viewport rather
        than assuming devicePixelRatio: browser zoom and emulation can make that assumption wrong.
        """
        scale = getattr(self, "_last_screenshot_scale", None)
        if not scale:
            return float(x), float(y)
        sx, sy = scale
        return float(x) / sx, float(y) / sy

    def click_xy(self, x, y):
        """Click a visual/canvas target using coordinates read from web_screenshot."""
        vx, vy = self._image_to_viewport(x, y)
        for t in ("mousePressed", "mouseReleased"):
            self._cmd("Input.dispatchMouseEvent",
                      {"type": t, "x": vx, "y": vy, "button": "left", "clickCount": 1})
        return f"clicked screenshot point ({x}, {y})"

    def drag_xy(self, from_x, from_y, to_x, to_y):
        """Drag between screenshot coordinates inside the renderer without moving the OS cursor."""
        x1, y1 = self._image_to_viewport(from_x, from_y)
        x2, y2 = self._image_to_viewport(to_x, to_y)
        self._cmd("Input.dispatchMouseEvent",
                  {"type": "mousePressed", "x": x1, "y": y1, "button": "left", "clickCount": 1})
        # A few intermediate events make canvas editors recognize a drag rather than a click.
        for i in range(1, 6):
            f = i / 5
            self._cmd("Input.dispatchMouseEvent",
                      {"type": "mouseMoved", "x": x1 + (x2 - x1) * f,
                       "y": y1 + (y2 - y1) * f, "button": "left", "buttons": 1})
        self._cmd("Input.dispatchMouseEvent",
                  {"type": "mouseReleased", "x": x2, "y": y2,
                   "button": "left", "clickCount": 1})
        return f"dragged screenshot point ({from_x}, {from_y}) to ({to_x}, {to_y})"

    # JS that sets a field's value the way the DOM expects: REPLACES existing content (fixes
    # the append bug), fires input+change so React/Vue register it, and handles native <select>
    # dropdowns (which CDP mouse events can't open) by matching an option by value/visible text.
    _SELECT_KIND_FN = r"""function(){
      var el=this, tag=(el.tagName||'').toLowerCase();
      var role=((el.getAttribute&&el.getAttribute('role'))||'').toLowerCase();
      if(tag==='option') return 'IS_OPTION';
      if(tag==='select' || role==='combobox' || role==='listbox') return 'IS_SELECT';
      return 'OK';
    }"""

    _SET_FN = r"""function(v){
      var el=this, tag=(el.tagName||'').toLowerCase();
      if(tag==='select'){
        var opts=[].slice.call(el.options), s=String(v);
        var o=opts.filter(function(o){return o.value===v||o.text.trim()===s;})[0]
           || opts.filter(function(o){return o.text.trim().toLowerCase()===s.toLowerCase();})[0]
           || opts.filter(function(o){return o.text.toLowerCase().indexOf(s.toLowerCase())>=0;})[0];
        if(!o) return 'NO_OPTION: "'+s+'" not found; options include: '
                 + opts.slice(0,15).map(function(o){return o.text.trim();}).join(' | ')
                 + ' — type one of those VISIBLE labels into this <select> ref; do not click options';
        el.value=o.value;
        el.dispatchEvent(new Event('input',{bubbles:true}));
        el.dispatchEvent(new Event('change',{bubbles:true}));
        return 'selected "'+o.text.trim()+'"';
      }
      if(tag==='input'||tag==='textarea'){
        var proto=(tag==='textarea')?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;
        var setter=Object.getOwnPropertyDescriptor(proto,'value').set;
        el.focus(); setter.call(el, v==null?'':String(v));
        el.dispatchEvent(new Event('input',{bubbles:true}));
        el.dispatchEvent(new Event('change',{bubbles:true}));
        return 'set';
      }
      return 'FALLBACK';
    }"""

    # Detect (and focus) an xterm.js terminal, returning 'TERMINAL' or 'NOT_TERMINAL'. A terminal
    # must NOT be typed via .value-setting (_SET_FN) — xterm reads keystrokes, not the textarea's
    # value — so a match reroutes to the keystroke path. Three cases, because xterm's real input
    # textarea is aria-hidden and does NOT appear in the a11y snapshot: the ref may be (a) that
    # helper textarea, (b) the .xterm container / a node inside it, or (c) the only terminal handle
    # the tree DOES expose — the "Terminal" tab/region. In case (c) we focus the ACTIVE terminal's
    # textarea (VS Code keeps only the visible terminal's xterm in the DOM), so typing on the tab
    # the agent can actually see still lands in the shell.
    _XTERM_FOCUS_FN = r"""function(){
      var el=this;
      var term=(el.closest&&el.closest('.xterm'))
            || ((el.classList&&el.classList.contains('xterm-helper-textarea'))?el:null)
            || (el.querySelector&&el.querySelector('.xterm'));
      var ta=null;
      if(term){
        ta=(el.classList&&el.classList.contains('xterm-helper-textarea'))?el
          :(term.querySelector&&term.querySelector('.xterm-helper-textarea'));
      } else {
        var label=(((el.getAttribute&&el.getAttribute('aria-label'))||'')+' '+((el.textContent)||'')).toLowerCase();
        if(/terminal/.test(label)) ta=document.querySelector('.xterm-helper-textarea');
      }
      if(!ta) return 'NOT_TERMINAL';
      ta.focus();
      return 'TERMINAL';
    }"""

    def _press_enter(self):
        for t in ("keyDown", "keyUp"):
            self._cmd("Input.dispatchKeyEvent", {"type": t, "key": "Enter", "code": "Enter",
                      "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r"})

    def _insert_stream(self, text):
        """Type `text` into whatever is focused, sending a real Enter for every newline. This is
        how a terminal (and any submit-on-Enter field) receives multi-line input: printable runs
        go in via Input.insertText, line breaks become Enter keystrokes. A trailing '\\n' SUBMITS
        (runs the command) — so type 'claude agents\\n' to start it, or omit the '\\n' to stage it."""
        segs = text.split("\n")
        for idx, seg in enumerate(segs):
            if seg:
                self._cmd("Input.insertText", {"text": seg})
            if idx < len(segs) - 1:
                self._press_enter()

    @staticmethod
    def _key_descriptor(ch):
        """Best-effort DOM key metadata; the char event's text is the source of truth."""
        if ch == "\n":
            return "Enter", "Enter", 13, "\r", "\r"
        if ch == " ":
            return " ", "Space", 32, " ", " "
        if len(ch) == 1 and ch.isascii() and ch.isalpha():
            return ch, "Key" + ch.upper(), ord(ch.upper()), ch, ch.lower()
        if len(ch) == 1 and ch.isascii() and ch.isdigit():
            return ch, "Digit" + ch, ord(ch), ch, ch
        return ch, "", 0, ch, ch

    def _type_at_focus(self, text):
        """Type into a focused rich/canvas editor as actual CDP keyboard events.

        Google Docs visibly places its caret after a renderer click but silently ignores
        Input.insertText. A rawKeyDown -> char -> keyUp sequence is the same path used by browser
        automation keyboards and is accepted by Docs, Slides, and conventional web editors.
        """
        for ch in text:
            key, code, vk, inserted, unmodified = self._key_descriptor(ch)
            base = {"key": key, "code": code, "windowsVirtualKeyCode": vk,
                    "nativeVirtualKeyCode": vk}
            # Do not pipeline across characters. Docs drops later input if the next key sequence
            # arrives before the renderer has completed the previous one.
            self._cmd("Input.dispatchKeyEvent", {**base, "type": "rawKeyDown"})
            self._cmd("Input.dispatchKeyEvent", {**base, "type": "char", "text": inserted,
                                                  "unmodifiedText": unmodified})
            self._cmd("Input.dispatchKeyEvent", {**base, "type": "keyUp"})

    def fill_secret(self, ref, text, expected_url):
        """Fill only a verified form input; never fall back to terminal/key events."""
        from urllib.parse import urlsplit
        parsed = urlsplit(expected_url)
        expected_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.validate_document()
        if ref:
            backend = self.registry.get(ref)
            if backend is None:
                return False
            obj = self._cmd("DOM.resolveNode", {"backendNodeId": backend}).get("object", {}).get("objectId")
        else:
            obj = self._cmd("Runtime.evaluate", {"expression": "document.activeElement"}).get("result", {}).get("objectId")
        if not obj:
            return False
        result = self._cmd("Runtime.callFunctionOn", {
            "objectId": obj, "arguments": [{"value": text}, {"value": expected_origin}],
            "returnByValue": True,
            "functionDeclaration": """function(value, origin) {
              if (location.origin !== origin || !this.isConnected || this.disabled || this.readOnly
                  || !this.matches('input,textarea') || this.closest('.xterm')
                  || /^(file|checkbox|radio|button|submit|reset|image|hidden)$/.test(this.type)) return false;
              const proto = this.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, value);
              this.dispatchEvent(new Event('input', {bubbles:true}));
              this.dispatchEvent(new Event('change', {bubbles:true}));
              return this.value === value;
            }"""})
        return result.get("result", {}).get("value") is True

    def type_text(self, ref, text):
        """Fill a field, REPLACING its content. Works for text inputs, textareas, and native
        <select> dropdowns (matches an option by visible text/value). An xterm.js TERMINAL
        (Cursor/VS Code integrated terminal) is detected and typed via real keystrokes into the
        PTY (newlines become Enter — a trailing newline runs the command). Falls back to focus +
        select-all + insertText for contenteditable / rich editors."""
        if not ref:
            self._type_at_focus(text)
            return f"typed {len(text)} chars at focus as keyboard events"
        backend = self.registry.get(ref)
        if backend is None:
            raise KeyError(f"stale ref {ref}")
        obj = self._cmd("DOM.resolveNode", {"backendNodeId": backend}).get("object", {}).get("objectId")
        if obj:
            kind = (self._cmd("Runtime.callFunctionOn",
                              {"objectId": obj, "functionDeclaration": self._XTERM_FOCUS_FN,
                               "returnByValue": True}).get("result", {}).get("value", ""))
            if kind == "TERMINAL":
                self._insert_stream(text)
                ran = " (ran it)" if text.endswith("\n") else " (staged — no trailing newline)"
                return f"{ref}: typed into the terminal as real keystrokes{ran}"
            res = (self._cmd("Runtime.callFunctionOn",
                             {"objectId": obj, "functionDeclaration": self._SET_FN,
                              "arguments": [{"value": text}], "returnByValue": True})
                   .get("result", {}).get("value", ""))
            if res and res != "FALLBACK":
                return f"{ref}: {res}"
        # fallback (contenteditable / non-standard): click to focus, select-all, replace
        x, y = self._center(ref)
        for t in ("mousePressed", "mouseReleased"):
            self._cmd("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
        time.sleep(0.1)
        for t in ("keyDown", "keyUp"):  # ⌘A select-all so insertText replaces the selection
            self._cmd("Input.dispatchKeyEvent", {"type": t, "key": "a", "code": "KeyA",
                                                 "windowsVirtualKeyCode": 65, "modifiers": 4})
        self._cmd("Input.insertText", {"text": text})
        return f"typed into {ref} (replaced)"

    def fill_login(self, username, password, expected_url=""):
        """Fill the visible login form's username + password fields. The values go STRAIGHT into
        the page over CDP and are NEVER returned — the caller (and thus the agent/LLM) only learns
        which fields were filled. Handles two-step logins by filling whatever visible fields exist
        (call again after advancing to the password step). Returns {'username_filled','password_filled'}."""
        from urllib.parse import urlsplit
        parsed = urlsplit(expected_url)
        expected_origin = f"{parsed.scheme}://{parsed.netloc}" if expected_url else ""
        self.validate_document()
        js = (
            "(function(u,p,origin){"
            "if(!origin || location.origin!==origin) return JSON.stringify({user:false,pass:false});"
            "function vis(el){var r=el.getBoundingClientRect();return r.width>0&&r.height>0&&el.offsetParent!==null;}"
            "function setVal(el,val){var proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;"
            "var setter=Object.getOwnPropertyDescriptor(proto,'value').set;el.focus();setter.call(el,val);"
            "el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}"
            "var pass=[].slice.call(document.querySelectorAll('input[type=password]')).filter(vis)[0]||null;"
            "var user=null,inps=[].slice.call(document.querySelectorAll('input')).filter(vis);"
            "if(pass){var pi=inps.indexOf(pass);for(var i=pi-1;i>=0;i--){var t=(inps[i].type||'').toLowerCase();"
            "if(t==='email'||t==='text'||t==='tel'){user=inps[i];break;}}}"
            "if(!user)user=[].slice.call(document.querySelectorAll("
            "'input[type=email],input[autocomplete=username],input[name*=email i],input[name*=user i],"
            "input[id*=email i],input[id*=user i],input[type=text],input[type=tel]')).filter(vis)[0]||null;"
            "var du=false,dp=false;if(user&&u){setVal(user,u);du=user.value===u;}if(pass&&p){setVal(pass,p);dp=pass.value===p;}"
            "return JSON.stringify({user:du,pass:dp});})("
            + json.dumps(username or "") + "," + json.dumps(password or "") + "," + json.dumps(expected_origin) + ")"
        )
        try:
            res = self._cmd("Runtime.evaluate", {"expression": js, "returnByValue": True})
            d = json.loads(res.get("result", {}).get("value", "") or "{}")
        except Exception:
            d = {}
        return {"username_filled": bool(d.get("user")), "password_filled": bool(d.get("pass"))}

    _KEYS = {"return": ("Enter", 13), "enter": ("Enter", 13), "tab": ("Tab", 9),
             "escape": ("Escape", 27), "backspace": ("Backspace", 8), "delete": ("Delete", 46),
             "up": ("ArrowUp", 38), "down": ("ArrowDown", 40),
             "left": ("ArrowLeft", 37), "right": ("ArrowRight", 39), "space": (" ", 32),
             # backtick: needed for ctrl+` — the toggle that opens an editor's integrated terminal
             "`": ("`", 192), "backtick": ("`", 192), "backquote": ("`", 192)}
    # DOM `code` for keys whose code != the derived Key*/Digit* (punctuation shortcuts). Without
    # the right code, VS Code/Cursor keybindings (which match on code) won't fire.
    _CODES = {"`": "Backquote"}
    # CDP modifier bitmask (Input.dispatchKeyEvent): Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8
    _MODBITS = {"alt": 1, "option": 1, "ctrl": 2, "control": 2,
                "meta": 4, "cmd": 4, "command": 4, "super": 4, "win": 4, "shift": 8}

    def press_key(self, key, modifiers=None):
        k, code = self._KEYS.get(key.lower(), (key, 0))
        # single letters/digits: derive the virtual key code + DOM code so combos like
        # cmd+Enter / cmd+A / ctrl+C actually register on the page
        codefield = k
        if code == 0 and len(k) == 1 and k.isalnum():
            code = ord(k.upper())
            codefield = ("Key" + k.upper()) if k.isalpha() else ("Digit" + k)
        codefield = self._CODES.get(k, codefield)
        mods = 0
        for m in (modifiers or []):
            mods |= self._MODBITS.get(str(m).lower(), 0)
        # with a modifier held it's a shortcut, not text entry -> rawKeyDown (no char event)
        down = "rawKeyDown" if mods else "keyDown"
        for t in (down, "keyUp"):
            self._cmd("Input.dispatchKeyEvent",
                      {"type": t, "key": k, "code": codefield, "modifiers": mods,
                       "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code})
        return "pressed " + "+".join([str(m) for m in (modifiers or [])] + [key])

    def mark(self):
        """Tag this window with a title + green banner so the user can tell the Hunch CDP
        window apart from their normal browser during an interactive login."""
        js = ("(()=>{document.title='\\u{1F7E2} HUNCH \\u2014 LOG IN HERE';"
              "let b=document.getElementById('hunch-banner');"
              "if(!b){b=document.createElement('div');b.id='hunch-banner';"
              "document.documentElement.appendChild(b);}"
              "b.textContent='\\u{1F7E2} THIS is the Hunch CDP window \\u2014 sign in HERE, then leave it open';"
              "b.setAttribute('style','position:fixed;top:0;left:0;right:0;z-index:2147483647;"
              "background:#0a7d34;color:#fff;font:700 18px system-ui;padding:14px;text-align:center;"
              "box-shadow:0 2px 8px rgba(0,0,0,.4)');})()")
        try:
            self._cmd("Runtime.evaluate", {"expression": js, "returnByValue": True})
        except Exception:
            pass

    def signed_out(self):
        """Heuristic: are we sitting on a login wall rather than the app itself?"""
        low = (self.url() + " " + self.title()).lower()
        return ("accounts.google.com" in low) or ("/signin" in low) or ("sign in" in low)

    def navigate(self, url):
        from .destinations import navigation_refusal
        refusal = navigation_refusal(url, self.allowed_origins)
        if refusal:
            return refusal
        self.registry.clear()
        self._last_screenshot_scale = None
        result = self._cmd("Page.navigate", {"url": url})
        if result.get("errorText"):
            return f"REFUSED: navigation failed: {result['errorText']}"
        time.sleep(2)
        # A guessed PATH on a valid host (e.g. a16z.com/apply) resolves but often 404s. If the landed
        # page looks like a not-found, say so and steer back to exploring from the homepage.
        try:
            probe = self._cmd("Runtime.evaluate", {"returnByValue": True, "expression":
                "(document.title+' '+((document.body&&document.body.innerText)||'').slice(0,400)).toLowerCase()"}
                ).get("result", {}).get("value", "")
        except Exception:
            probe = ""
        if any(k in probe for k in ("404", "page not found", "page can't be found", "can’t be found",
                                    "cannot be found", "doesn't exist", "no longer available")):
            return (f"navigated to {url} — but the page looks like a 404 / not-found. Don't guess paths: "
                    "open the site's HOMEPAGE, web_snapshot it, and CLICK through its real links to the goal.")
        return f"navigated to {url}"

    def ready(self):
        """Has the document finished loading? (readyState == 'complete')"""
        try:
            v = self._cmd("Runtime.evaluate",
                          {"expression": "document.readyState", "returnByValue": True})
            return v.get("result", {}).get("value") == "complete"
        except Exception:
            return False

    def wait_ready(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if self.ready():
                return True
            time.sleep(0.5)
        return False

    def capture_screenshot(self):
        """PNG of the CDP page itself (base64), via Page.captureScreenshot — the FOCUS-FREE way to
        see a background web page as pixels. The OS `screenshot` tool grabs the physical frontmost
        screen instead, so on a background CDP window it captures the USER's own window, not this
        page. Use this for genuinely visual web content (a chart/canvas/image) the tree can't convey."""
        self._follow_new_tab()
        r = self._cmd("Page.captureScreenshot", {"format": "png"})
        data = r.get("data", "")
        # Save a mapping for subsequent click_xy/drag actions. PNG's IHDR stores width/height
        # at bytes 16..24; no image dependency is needed just to read it.
        try:
            raw = base64.b64decode(data)
            if raw[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("not PNG")
            image_w, image_h = struct.unpack(">II", raw[16:24])
            viewport = self._cmd("Runtime.evaluate", {
                "expression": "JSON.stringify([window.innerWidth,window.innerHeight])",
                "returnByValue": True,
            }).get("result", {}).get("value", "")
            viewport_w, viewport_h = json.loads(viewport)
            if image_w and image_h and viewport_w and viewport_h:
                self._last_screenshot_scale = (image_w / viewport_w, image_h / viewport_h)
        except Exception:
            self._last_screenshot_scale = (1.0, 1.0)
        return data

    def close(self):
        if self.ws:
            self.ws.close()


# ── CDPComputer: the agent-facing wrapper (same tools/handle contract) ────────
from .tool_registry import BASE_TOOLS
CDP_TOOLS = [{**tool, "name": tool["name"].removeprefix("web_")} for tool in BASE_TOOLS
             if tool["name"] in {"web_snapshot", "web_act"}]


class CDPComputer:
    """Drives a Chromium/Electron app over CDP, focus-free. Same snapshot/act/handle
    contract as local_mac.LocalComputer, so it slots into the same agent loop —
    but for the whole Chromium/Electron category, in the background."""

    def __init__(self, app, port=9333, url=None, isolated=False, background=True,
                 connect=True, profile=None, editor=False, allowed_origins=(), owner=None):
        self.app = app
        self.port = port
        self.editor = editor
        self.tools = CDP_TOOLS
        self.session = None
        self.launch_note = ""
        self.identity = None
        if connect:
            self.launch_note = launch_chromium(app, port, url=url, background=background,
                                               isolated=isolated, profile=profile, editor=editor,
                                               allowed_origins=allowed_origins, owner=owner)
            # editors expose several page targets — bind the workbench (holds the tree + terminal)
            self.session = CDPSession(port).connect(editor=editor,
                                                    folder=url if editor else None)
            self.identity = _OWNED_ENDPOINTS[port]["identity"]
            self.session.allowed_origins = allowed_origins
            self.session.pinned_app = _resolve_app(app) not in _BROWSER_ALIASES.values()

    def snapshot(self):
        return self.session.snapshot()[0]

    def act(self, actions, detailed=False, postcondition=None):
        from .results import action_receipt, validate_postcondition
        validate_postcondition(postcondition)
        lines = []
        for a in actions:
            act = a.get("action")
            try:
                self.session.validate_document()
                if act == "click":
                    lines.append(self.session.click(a["ref"]))
                elif act == "click_xy":
                    lines.append(self.session.click_xy(a["x"], a["y"]))
                elif act == "drag":
                    lines.append(self.session.drag_xy(a["from_x"], a["from_y"],
                                                      a["to_x"], a["to_y"]))
                elif act == "type":
                    lines.append(self.session.type_text(a.get("ref"), a.get("text", "")))
                elif act == "key":
                    lines.append(self.session.press_key(a["key"], a.get("modifiers")))
                elif act == "navigate":
                    lines.append(self.session.navigate(a["url"]))
                    break
                else:
                    lines.append(f"unknown action {act}")
                if lines and any(marker in lines[-1].lower() for marker in ("refused", "stale", "failed")):
                    break
                time.sleep(0.4)
            except Exception as e:  # noqa: BLE001
                lines.append(f"error on {act}: {e}")
                break
        time.sleep(0.6)
        if detailed or postcondition is not None:
            def read(ref, field):
                self.session.validate_document()
                backend = self.session.registry.get(ref)
                if backend is None:
                    raise ValueError("stale postcondition ref; observe the new document")
                obj = self.session._cmd("DOM.resolveNode", {"backendNodeId": backend}).get("object", {}).get("objectId")
                if not obj:
                    raise ValueError("postcondition element unavailable")
                expressions = {"value": "this.value", "title": "this.getAttribute('title')",
                               "enabled": "!this.disabled", "selected": "this.selected ?? this.checked ?? this.getAttribute('aria-selected')",
                               "expanded": "this.getAttribute('aria-expanded')"}
                value = self.session._cmd("Runtime.callFunctionOn", {
                    "objectId": obj, "functionDeclaration": "function(){ return " + expressions[field] + "; }",
                    "returnByValue": True}).get("result", {})
                if "value" not in value:
                    raise ValueError("postcondition field unavailable")
                return value["value"]
            outcome = action_receipt(lines, len(actions), "cdp", {
                "identity": self.identity, "renderer": self.session.target_id}, {}, postcondition, read)
            return {**outcome, "observation": self.snapshot()}
        return "Executed:\n" + "\n".join(lines) + "\n\nScreen now:\n" + self.snapshot()

    def handle(self, tool_use):
        name = tool_use.name if hasattr(tool_use, "name") else tool_use["name"]
        args = tool_use.input if hasattr(tool_use, "input") else tool_use.get("input", {})
        tid = tool_use.id if hasattr(tool_use, "id") else tool_use["id"]
        try:
            if name == "snapshot":
                content = self.snapshot()
            elif name == "act":
                content = self.act(args["actions"], detailed=args.get("detailed", False),
                                   postcondition=args.get("postcondition"))
            else:
                return {"type": "tool_result", "tool_use_id": tid, "content": f"unknown tool {name}", "is_error": True}
            return {"type": "tool_result", "tool_use_id": tid, "content": content}
        except Exception as e:  # noqa: BLE001
            return {"type": "tool_result", "tool_use_id": tid, "content": f"error: {e}", "is_error": True}
