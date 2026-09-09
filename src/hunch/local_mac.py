"""Hunch local backend (macOS) — drive THIS machine via the Accessibility API.

The same tree-primary approach as the Docker sandbox, but pointed at your real,
logged-in laptop: perception via AXUIElement, actions via AXPress (which triggers
an element WITHOUT moving your cursor) with a CGEvent coordinate fallback. Exposes
the same snapshot/act/screenshot interface as computer.py, so the agent loop is
identical — only the backend changes.

Requires: Accessibility permission granted to the running process
(System Settings → Privacy & Security → Accessibility), and pyobjc.
"""

import os
import base64
import json
import re
import subprocess
import tempfile
import time

from . import ax_tree_mac as ax  # reuse the AX primitives (get_attrs, bounds, list_apps, get_window)
from .notify import as_str  # AppleScript string escaping (canonical home is notify.py)
from ApplicationServices import (
    AXUIElementCreateApplication, AXUIElementPerformAction,
    AXUIElementSetAttributeValue, AXUIElementSetMessagingTimeout,
    AXUIElementCreateSystemWide, kAXFocusedAttribute, kAXValueAttribute,
    AXIsProcessTrusted, AXValueCreate, kAXValueCGPointType, kAXValueCGSizeType,
)
from AppKit import NSWorkspace, NSRunningApplication
from Quartz import CGPoint, CGSize
import Quartz


# Framework detection identifies candidate recovery mechanisms, not AX/CDP availability.
_EMBEDDED_CHROMIUM = ("Electron Framework.framework", "Chromium Embedded Framework.framework",
                     "Codex Framework.framework")


def _embedded_chromium(pid):
    """Return a bundled Chromium framework, without classifying operation coverage."""
    try:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        url = app.bundleURL() if app else None
        if not url:
            return None
        fw = os.path.join(url.path(), "Contents", "Frameworks")
        for name in _EMBEDDED_CHROMIUM:
            if os.path.exists(os.path.join(fw, name)):
                return name
    except Exception:
        pass
    return None


def _is_editor_terminal(el, pid):
    """True if `el` is an integrated TERMINAL inside an embedded-Chromium editor (Cursor,
    VS Code, …). Such terminals are xterm.js: they read real KEYSTROKES off a hidden helper
    textarea, so an AX value-set lands in xterm's screen-reader MIRROR and never reaches the
    PTY — the set 'succeeds' (returns 0) while nothing runs. We detect it so set_text refuses
    honestly and steers to the CDP path instead of reporting a phantom success. Narrow by
    design: only fires inside Electron/CEF apps, only on text elements, only when the AX name
    is xterm's 'Terminal N, …' label — so a normal Electron input (Slack, etc.) is untouched."""
    if not _embedded_chromium(pid):
        return False
    try:
        a = ax.get_attrs(el, (ax.kAXRoleAttribute, ax.kAXTitleAttribute, ax.kAXDescriptionAttribute))
        role = str(a.get(ax.kAXRoleAttribute) or "")
        if role not in ("AXTextArea", "AXTextField"):
            return False
        label = f"{a.get(ax.kAXTitleAttribute) or ''} {a.get(ax.kAXDescriptionAttribute) or ''}".lower()
    except Exception:
        return False
    # xterm labels the terminal "Terminal <n>, <shell/title>" (verified: "Terminal 1, zsh").
    return bool(re.search(r"\bterminal\b", label))


# Explicit recovery option for embedded Chromium. Its effect must be measured for
# the selected app/version/operation; a flag is not proof of background coverage.
_FORCE_AX_FLAG = "--force-renderer-accessibility"


def _proc_has_force_ax(pid):
    """True if the running process was launched with --force-renderer-accessibility."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout
        return _FORCE_AX_FLAG in out
    except Exception:
        return False


def _proc_cmdline(pid):
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""


def _twin_process_warning(app_name, pid):
    """A one-line warning when SEVERAL processes share this app's name — one of them Hunch's own
    CDP-driven copy (a browser/editor launched on a dedicated ~/.hunch profile).

    `_resolve_app` has to pick one, and its pick is not the CDP session's: the AX tools then read
    one 'Cursor' while web_snapshot/web_act drive another. Both look right in isolation, and every
    read comes back with a real tree — of the wrong instance. That silent split is unrecoverable
    from the tree alone, so it is called out on the snapshot itself.

    Cached briefly: this runs on every snapshot, and the process set barely moves."""
    hit = _twin_cache.get((app_name, pid))
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    try:
        pids = [p for p in _pids_named(app_name) if p != pid]
    except Exception:
        return ""
    msg = _twin_warning(app_name, pid, pids)
    _twin_cache[(app_name, pid)] = (time.time(), msg)
    return msg


_twin_cache = {}


def _twin_warning(app_name, pid, pids):
    if not pids:
        return ""
    hunch_owned = [p for p in pids if "/.hunch/" in _proc_cmdline(p)]
    mine = "/.hunch/" in _proc_cmdline(pid) if pid else False
    if hunch_owned and not mine:
        return (f"[!] {len(pids) + 1} processes named {app_name!r} are running. This tree is the "
                f"USER's copy (pid {pid}). Hunch's own CDP-driven copy is pid "
                f"{hunch_owned[0]} — the one web_snapshot/web_act drive. Acting here does NOT "
                f"affect that window, and vice versa. Use web_snapshot for the Hunch copy.")
    if mine:
        return (f"[!] {len(pids) + 1} processes named {app_name!r} are running. This tree is "
                f"HUNCH's CDP-driven copy (pid {pid}), not the user's own window.")
    return (f"[!] {len(pids) + 1} processes named {app_name!r} are running (pids "
            f"{', '.join(str(p) for p in [pid] + pids if p)}); this tree is pid {pid}. If it shows "
            f"the wrong window, the other process is the one you want.")


def _norm_app_name(s):
    """Fold an app name for matching. Some apps prepend invisible bidi/zero-width marks to their
    display name (WhatsApp's localizedName is really '\\u200eWhatsApp'), so an exact == against what
    the user typed silently fails ('app not found'). Strip those, trim, casefold."""
    for cp in (0x200E, 0x200F, 0x200B, 0x200C, 0x200D, 0xFEFF,
               0x202A, 0x202B, 0x202C, 0x202D, 0x202E):
        s = s.replace(chr(cp), "")
    return s.strip().casefold()


def _running_identity(pid):
    """Fresh stable identity for a running app; display names are not unique."""
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return {}
    url = app.bundleURL()
    from .targets import bundle_metadata
    return {
        "pid": pid,
        "started_at": str(app.launchDate() or ""),
        "activation_policy": int(app.activationPolicy()),
        "bundle_id": str(app.bundleIdentifier() or ""),
        "path": str(url.path() if url else ""),
        "name": str(app.localizedName() or ""),
        **(bundle_metadata(str(url.path())) if url else {}),
    }


def _resolve_app(app_name):
    """Resolve a single process, preserving identity and refusing ambiguity."""
    from .targets import select_running
    if str(app_name).startswith("pid:"):
        try:
            return _running_identity(int(str(app_name)[4:])) or None
        except ValueError:
            return None
    apps = [{**a, **_running_identity(a["pid"])} for a in ax.list_apps()]
    return select_running(app_name, apps)


def _pids_named(name):
    """PIDs of processes whose executable name is exactly `name`, read FRESH via pgrep. We can't use
    NSWorkspace.runningApplications() for liveness/relaunch polling: without a spinning NSRunLoop its
    list is cached at first read, so a just-killed app still appears running (this silently broke the
    force-accessibility relaunch — the kill worked but the poll never saw the app leave)."""
    try:
        out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=3).stdout
        return [int(x) for x in out.split()]
    except Exception:
        return []


def _process_alive(pid):
    """Fresh kernel liveness; NSRunningApplication termination state can be cached."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _enable_manual_ax(ax_app):
    """Request manual accessibility. Coverage and persistence need separate observation."""
    try:
        AXUIElementSetAttributeValue(ax_app, "AXManualAccessibility", True)
    except Exception:
        pass


# Deterministically alert the user whenever Hunch is about to bring an app to the front (a real
# focus switch). ON BY DEFAULT so it works under any host (the standalone app, Claude Code, Claude
# Desktop) — set HUNCH_NOTIFY_FOCUS=0 to silence.
def _notify_focus_enabled():
    # read at call time (not import time) so the env var applies however late it's set
    return os.environ.get("HUNCH_NOTIFY_FOCUS", "1") != "0"

# The agent's stated WHY for the next focus switch (set at the MCP tool layer, where the reason
# is known; consumed by the next _announce_front so the warning explains itself).
_focus_reason = ""


def set_focus_reason(reason):
    global _focus_reason
    _focus_reason = str(reason or "").strip()


# When the user just clicked "Go ahead" on a screen dialog, the follow-up focus notification
# would re-announce the very thing they approved — suppress it briefly so a switch is either
# asked about (dialog) or announced (notification), never both.
_suppress_until = 0.0


def suppress_focus_notice(seconds=15):
    global _suppress_until
    _suppress_until = time.monotonic() + seconds


def _notify_focus(name, reason=""):
    try:
        from .notify import notify
        n, why = str(name), str(reason)
        body = f"Bringing {n} to the front — {why}" if why else f"Bringing {n} to the front…"
        notify(body, "Hunch — focus", timeout=3)
    except Exception:
        pass


def _announce_front(name):
    """Tell the user BEFORE Hunch fronts an app — but only on a REAL switch (skip if it's already
    frontmost). The single choke point for EVERY focus-stealing path (activate, focus_app, and a
    foreground launch_app), so a focus shift is never silent."""
    global _focus_reason
    reason, _focus_reason = _focus_reason, ""   # consume — a stale reason must not label a later switch
    if not _notify_focus_enabled() or not name:
        return
    if time.monotonic() < _suppress_until:
        return  # the user just approved this switch in a dialog — don't re-announce it
    if _frontmost()[0] == name:
        return  # already frontmost — no switch happens
    _notify_focus(name, reason)

_MAX_NODES = 3000          # default node budget for a FULL-window walk
_MAX_DEPTH = 18            # default depth for a FULL-window walk
_SCOPED_MAX_NODES = 10000  # defaults for scoped (ref=) subtree walks and find()
_SCOPED_MAX_DEPTH = 100    # "full depth" while still guarding Python recursion
# HARD recursion guard, independent of max_depth. max_depth counts only EMITTED
# nodes (scaffolding is walked "for free" so the visible tree isn't dominated by
# wrapper groups), so a long enough chain of uninteresting containers recurses
# without ever advancing `depth`. The node budget is then the only backstop, and
# with _MAX_NODES=3000 > CPython's 1000-frame limit it loses the race: a real
# System Settings pane raised RecursionError mid-walk and killed snapshot()
# outright (2026-08-06). Truncate here instead — a capped tree beats a crash.
_MAX_RECURSION = 300
_NODES_CEILING = 50000     # hard safety ceiling everywhere
_MAX_CHILDREN = 200        # per-node sibling cap for a FULL-window walk (marked + recoverable)
_SCOPED_MAX_CHILDREN = 1000  # raised cap for scoped (ref=) walks and find(): page a big list in
# Cap on a single element's text value in the serialized tree. Must be generous: chat messages,
# code blocks, notes, and email bodies are real content the agent needs IN FULL (the old 120-char
# cap silently truncated Discord messages so Hunch could neither read nor copy them). Only truly
# pathological values (a giant textarea) get clipped — and then WITH a visible marker, never silently.
_MAX_VALUE_CHARS = 4000

_TRUNC_FOOTER = ("…tree truncated at {n} nodes — use snapshot(ref=...) on a container or "
                 "find(role=..., name_contains=...) to see more")


def _cap_depth(v, default):
    """None/negative -> default; 0 is honored (root only)."""
    return default if (v is None or v < 0) else v


def _cap_nodes(v, default):
    """None/0/negative -> default; always clamped to the safety ceiling."""
    v = default if (v is None or v <= 0) else v
    return min(v, _NODES_CEILING)


def _cap_children(v, default):
    """None/0/negative -> default; always clamped to the safety ceiling."""
    v = default if (v is None or v <= 0) else v
    return min(v, _NODES_CEILING)

# Interactive + content roles the agent can act on or read; everything else
# (AXGroup / AXScrollArea / AXSplitGroup scaffolding) is traversed but not shown.
_INTERACTIVE = {
    "AXButton", "AXTextField", "AXTextArea", "AXSecureTextField", "AXSearchField",
    "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXMenuItem",
    "AXLink", "AXComboBox", "AXSlider", "AXIncrementor", "AXStepper", "AXTabButton",
    "AXDisclosureTriangle", "AXColorWell",
}
_CONTENT = {"AXStaticText", "AXHeading", "AXCell", "AXValueIndicator"}
_ANCHOR = {"AXWindow", "AXSheet", "AXDialog"}
# Selectable list/table units. Their AXSelected is settable (so `select` works),
# but their NAME lives in child cells/text — so we emit them as ONE line labelled
# with the accessible name computed from their subtree, instead of dropping the
# row and surfacing only its unselectable text leaf. General to any list/outline.
_ROW_ROLES = {"AXRow", "AXOutlineRow", "AXListItem"}

_ATTRS = (ax.kAXRoleAttribute, ax.kAXTitleAttribute, ax.kAXDescriptionAttribute,
          ax.kAXValueAttribute, ax.kAXEnabledAttribute, ax.kAXPositionAttribute,
          ax.kAXSizeAttribute, ax.kAXChildrenAttribute)


def _frontmost():
    """Fresh (name, pid) of the frontmost app, straight from LaunchServices.
    NSWorkspace.frontmostApplication() must NOT be used for this: it is KVO-cached
    and never updates in a process that isn't pumping a run loop (this MCP server,
    any plain script) — activate()'s settle loop saw a frozen value, concluded the
    raise failed, and act() then refused shared-input actions that would have worked."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        if not asn:
            return (None, None)
        info = subprocess.run(["lsappinfo", "info", "-only", "name,pid", asn],
                              capture_output=True, text=True, timeout=2).stdout
        name = re.search(r'"?(?:LSDisplayName|name)"?\s*=\s*"([^"]*)"', info)
        pid = re.search(r'"?pid"?\s*=\s*(\d+)', info)
        return (name.group(1) if name else None, int(pid.group(1)) if pid else None)
    except Exception:
        return (None, None)


class StaleRef(Exception):
    pass


class MacSession:
    """Per-snapshot ref registry mapping [e1..eN] to LIVE AXUIElement handles, so
    the agent acts on real elements (AXPress) not coordinates."""

    def __init__(self):
        AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 2.0)
        self.registry = {}
        self._keymap = {}
        self._ref_keys = {}  # ref -> full key path; mirrors _keymap (persistent) so a
        #                      scoped snapshot can seed the subtree root's exact key
        self._counter = 0
        self.snapshot_count = 0
        self._pid = None  # target app pid, for activation before acting
        self._app_name = None  # target app name; set by snapshot(), read by activate()/window ops
        # Shared-input use — the receipt behind "focus-free": every path that touches
        # the ONE cursor/keyboard or raises an app increments these; act() reports the
        # per-call delta so a disturbance is never silent in the transcript either.
        self.disturbances = {"pixel_clicks": 0, "keystrokes": 0, "key_combos": 0,
                             "app_raises": 0, "drags": 0}
        # The walk is SERIAL by design. Batching (get_attrs = all attrs of one
        # element in a single Mach round-trip) is the real IPC win; reading a tree
        # across threads does NOT help, because the target app answers AX on its one
        # main thread — concurrent reads of a single app's tree can't overlap (this
        # is server-side, not the GIL, which the pyobjc call does release). Measured
        # in bench/ax_traversal_bench.py, ax_bottleneck_probe.py, ax_multiapp_probe.py.

    def activate(self):
        """Bring the target app to the front so its window is discoverable and
        keystrokes route to it. Returns True only if the app is actually frontmost.
        Crucially, if it is ALREADY frontmost we do nothing — re-activating an app
        dismisses its open context menus/popups (which breaks right_click -> menu)."""
        if not self._pid:
            return False
        if not self._validate_target():
            return False
        guard = getattr(self, "_shared_guard", None)
        if guard and guard():
            return False
        if _frontmost()[1] == self._pid:
            return True  # already active — no focus switch happens
        _announce_front(getattr(self, "_app_name", None) or "an app")  # deterministic focus warning
        # Cooperative activation may be refused; address the exact process and
        # verify the foreground PID before posting any shared input.
        deadline = time.time() + 4.0
        while time.time() < deadline:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(self._pid)
            if app:
                if not app.activateWithOptions_(2):
                    _activate_process(self._pid)
            else:
                _activate_process(self._pid)
            settle = time.time() + 1.0
            while time.time() < settle:
                if _frontmost()[1] == self._pid:
                    self.disturbances["app_raises"] += 1
                    return True
                time.sleep(0.1)
        return False

    def _ref_for(self, key):
        ref = self._keymap.get(key)
        if ref is None:
            self._counter += 1
            ref = f"e{self._counter}"
            self._keymap[key] = ref
            self._ref_keys[ref] = key   # single write site: every registry write mints here first
        return ref

    def _accessible_name(self, el, cap=8):
        """A container's label computed from its own + descendant text (bounded) —
        the accessible-name pattern, so a nameless AXRow can be labelled by the
        filename in its cell."""
        parts, budget = [], [cap]
        def collect(e, d):
            if budget[0] <= 0 or d > 5:
                return
            budget[0] -= 1
            a = ax.get_attrs(e, (ax.kAXTitleAttribute, ax.kAXValueAttribute, ax.kAXChildrenAttribute))
            for x in (a[ax.kAXTitleAttribute], a[ax.kAXValueAttribute]):
                s = str(x or "").strip()
                if s and s not in parts:
                    parts.append(s)
            for c in (a[ax.kAXChildrenAttribute] or []):
                collect(c, d + 1)
        collect(el, 0)
        return " ".join(parts)[:100]

    def _describe(self, el, a):
        """Unpack a fetched attrs dict into display fields. `name` applies the row
        rule (title, else value, else accessible name from the subtree)."""
        role = str(a[ax.kAXRoleAttribute] or "?")
        title = str(a[ax.kAXTitleAttribute] or "").strip()
        desc = str(a[ax.kAXDescriptionAttribute] or "").strip()
        v = a[ax.kAXValueAttribute]
        value = str(v).strip() if v is not None else ""
        enabled = a[ax.kAXEnabledAttribute]
        is_row = role in _ROW_ROLES
        name = (title or value or self._accessible_name(el)) if is_row else title
        return role, title, desc, value, enabled, name, is_row

    @staticmethod
    def _seg_key(parent_key, sib, role, name, desc):
        """One stable path segment: role+name+desc, disambiguated by sibling
        occurrence so two same-named siblings still get distinct refs. Mutates sib."""
        seg = f"{role}:{name}:{desc}"
        occ = sib.get(seg, 0)
        sib[seg] = occ + 1
        return f"{parent_key}/{seg}#{occ}"

    @staticmethod
    def _fmt_el(ref, role, title, desc, value, enabled):
        parts = [f"[{ref}]", role]
        if title:
            parts.append(f'"{title}"')
        if desc and desc != title:
            parts.append(f"({desc})")
        if value and value != title:
            shown = value if len(value) <= _MAX_VALUE_CHARS else \
                value[:_MAX_VALUE_CHARS] + f"…(+{len(value) - _MAX_VALUE_CHARS} more chars)"
            parts.append(f"val={shown!r}")
        if enabled is False:
            parts.append("disabled")
        return " ".join(parts)

    @staticmethod
    def _interesting(role, title, value):
        # General filter: keep interactive controls and content-bearing text; drop
        # structural scaffolding. Nothing app-specific — the agent reasons about
        # whatever the tree surfaces.
        if role in _ANCHOR or role in _INTERACTIVE:
            return True
        if role in _CONTENT and (title or value):
            return True
        return False

    def snapshot(self, app_name=None, compact=True, max_depth=None, activate_app=True,
                 ref=None, max_nodes=None, max_children=None):
        """Compact tree of the selected window, else the app's focused window
        (frontmost app by default). Returns (text, info) and refreshes refs.
        Pass ref='eNN' to re-walk ONLY that element's subtree (deeper defaults,
        does NOT clear other refs). max_depth/max_nodes/max_children=None ->
        defaults; when a cap truncates output, an explicit …marker line says what
        was dropped and how to see more."""
        if ref is not None:
            return self._snapshot_scoped(ref, max_depth, max_nodes, compact, max_children)
        max_depth = _cap_depth(max_depth, _MAX_DEPTH)
        max_nodes = _cap_nodes(max_nodes, _MAX_NODES)
        max_children = _cap_children(max_children, _MAX_CHILDREN)
        self.registry = {}
        self.snapshot_count += 1
        win, app_name, err = self._resolve_window(app_name)
        if err is not None:
            return err
        scope = "selected window" if getattr(self, "_selected_window", None) else "focused window"
        lines = [f"=== {app_name} — {scope} (snapshot #{self.snapshot_count}) ==="]
        warn = _twin_process_warning(app_name, getattr(self, "_pid", None))
        if warn:
            lines.append(warn)
        budget = {"left": max_nodes, "hit": False}
        self._walk(win, 0, "", lines, compact, max_depth, budget, max_children=max_children)
        if budget["hit"]:
            lines.append(_TRUNC_FOOTER.format(n=max_nodes))
        text = "\n".join(lines)
        return text, {"est_tokens": round(len(text) / 3.5), "refs": len(self.registry), "app": app_name}

    def _resolve_window(self, app_name):
        """Resolve app -> (window element, real app name, error). On failure the
        error is the (text, info) tuple snapshot()/find() should return as-is.
        Observation never launches, quits, or activates an application."""
        from .targets import TargetError, process_key
        match = None
        if app_name is None:
            app_name, pid = _frontmost()
        else:
            try:
                match = _resolve_app(app_name)
            except TargetError as error:
                self._invalidate_refs()
                return None, app_name, (str(error), {"refs": 0, "app": app_name})
            pid = match["pid"] if match else None
            if match:
                app_name = match["name"]   # use the app's REAL name (with any invisible marks) downstream
        if pid is None:
            self._invalidate_refs()
            return None, app_name, (f"(app {app_name!r} not found)",
                                    {"est_tokens": 20, "refs": 0, "app": app_name})
        self._pid = pid
        self._app_name = app_name  # used by activate() for reliable `open -a` fronting
        identity = match or {"pid": pid, "name": app_name, **_running_identity(pid)}
        if process_key(identity) != process_key(getattr(self, "_identity", {})):
            self._invalidate_refs()
        self._identity = identity
        ax_app = AXUIElementCreateApplication(pid)
        if (AXIsProcessTrusted() and _embedded_chromium(pid)
                and getattr(self, "_manual_ax_identity", None) != process_key(identity)):
            _enable_manual_ax(ax_app)
            self._manual_ax_identity = process_key(identity)
        win = ax.get_window(ax_app)
        selected = getattr(self, "_selected_window", None)
        # Resolve aliases before deciding whether the caller switched apps. A
        # name/bundle/path for the selected PID must not unpin its window just
        # because app_target stored a pid:N selector. Failed resolution above
        # leaves the binding intact; a reused PID still fails the identity check.
        if selected and selected[0][0] != pid:
            self._selected_window = selected = None
        if selected:
            observations, _ = ax.discover_windows(ax_app)
            if selected[0] != process_key(identity) or not any(w[0] == selected[1] for w in observations):
                self._invalidate_refs()
                return None, app_name, ("selected window is stale; inspect targets again", {"refs": 0})
            win = selected[1]
        if win != getattr(self, "_window", None):
            self._invalidate_refs()
        self._window = win
        if win is None:
            if not AXIsProcessTrusted():
                return None, app_name, (
                    (f"(no window for {app_name} — and this process is NOT trusted for "
                     "Accessibility, so ALL tree reads will come back empty. Tell the user: "
                     "grant the app hosting Hunch in System Settings → Privacy & Security → "
                     "Accessibility — toggle it off and on if it's already listed — then "
                     "restart the host. `hunch doctor` explains.)"),
                    {"est_tokens": 60, "refs": 0, "app": app_name})
            return None, app_name, (
                (f"(no AX window found for {app_name}, PID {pid}, in the current state. "
                 "This does not establish that the app has no accessibility support. "
                 "Check the selected process and open windows; an existing CDP endpoint or "
                 "an authorized accessibility/debugging relaunch may provide another surface.)"),
                {"est_tokens": 40, "refs": 0, "app": app_name})
        return win, app_name, None

    def _invalidate_refs(self):
        self.registry.clear()
        self._keymap.clear()
        self._ref_keys.clear()

    def _validate_target(self):
        from .targets import process_key
        identity = getattr(self, "_identity", None)
        if identity and process_key(_running_identity(identity["pid"])) != process_key(identity):
            self._invalidate_refs()
            return False
        window = getattr(self, "_window", None)
        if identity and window is not None:
            windows, _ = ax.discover_windows(AXUIElementCreateApplication(identity["pid"]))
            if not any(item[0] == window for item in windows):
                self._invalidate_refs()
                return False
        return True

    def _snapshot_scoped(self, ref, max_depth, max_nodes, compact, max_children=None):
        """Re-walk ONLY the subtree under a known ref, at generous depth. Does NOT
        clear the registry: every other ref stays live, subtree refs are updated in
        place (the persistent _keymap makes them identical to full-walk refs).
        The sibling cap defaults HIGHER here (_SCOPED_MAX_CHILDREN) so scoping into
        a big list's container pages in far more rows than the full-window walk."""
        self._validate_target()
        el = self.registry.get(ref)
        key = self._ref_keys.get(ref)
        app = getattr(self, "_app_name", None) or "app"
        if el is None or key is None:
            return (f"(ref {ref} unknown or stale — take a full snapshot first)",
                    {"est_tokens": 15, "refs": len(self.registry), "app": app})
        a = ax.get_attrs(el, _ATTRS)
        if a[ax.kAXRoleAttribute] is None:
            return (f"(ref {ref} is stale — its element is gone; re-snapshot the app)",
                    {"est_tokens": 15, "refs": len(self.registry), "app": app})
        max_depth = _cap_depth(max_depth, _SCOPED_MAX_DEPTH)
        max_nodes = _cap_nodes(max_nodes, _SCOPED_MAX_NODES)
        max_children = _cap_children(max_children, _SCOPED_MAX_CHILDREN)
        self.snapshot_count += 1
        lines = [f"=== {app} — subtree of [{ref}] (snapshot #{self.snapshot_count}) ==="]
        budget = {"left": max_nodes, "hit": False}
        self._walk(el, 0, "", lines, compact, max_depth, budget, root_key=key,
                   max_children=max_children)
        if budget["hit"]:
            lines.append(_TRUNC_FOOTER.format(n=max_nodes))
        text = "\n".join(lines)
        return text, {"est_tokens": round(len(text) / 3.5), "refs": len(self.registry), "app": app}

    def find(self, role=None, name_contains=None, max_results=20, app_name=None, max_nodes=None):
        """Search the WHOLE tree (deeper than snapshot shows) for matching elements.
        Registers refs for matches only — does NOT clear existing refs. Returns
        (text, info): one line per match with an ancestor breadcrumb."""
        win, app_name, err = self._resolve_window(app_name)
        if err is not None:
            return err
        max_nodes = _cap_nodes(max_nodes, _SCOPED_MAX_NODES)
        max_depth = _SCOPED_MAX_DEPTH
        want_role = role.lower() if role else None
        if want_role and want_role.startswith("ax"):
            want_role = want_role[2:]
        needle = (name_contains or "").lower()
        results, searched, more = [], [0], [False]

        def matches(role_, title, desc, value, name):
            if want_role is not None:
                r = role_.lower()
                if (r[2:] if r.startswith("ax") else r) != want_role:
                    return False
            if needle:
                hay = " ".join(x for x in (title, desc, value, name) if x).lower()
                return needle in hay
            return True

        def walk(el, depth, parent_key, sib, crumbs):
            if len(results) >= max_results or searched[0] >= max_nodes:
                more[0] = True
                return
            searched[0] += 1
            a = ax.get_attrs(el, _ATTRS)
            role_, title, desc, value, enabled, name, is_row = self._describe(el, a)
            key = self._seg_key(parent_key, sib, role_, name, desc)
            if matches(role_, title, desc, value, name):
                r = self._ref_for(key)
                self.registry[r] = el
                crumb = " > ".join(crumbs[-4:])
                line = self._fmt_el(r, role_, title or name, desc, value, enabled)
                results.append((crumb + " › " if crumb else "") + line)
            if depth >= max_depth:
                return
            children = a[ax.kAXChildrenAttribute]
            if not children:
                return
            shown = (title or name)[:40]
            child_crumbs = crumbs + [shown] if shown else crumbs
            child_sib = {}
            # Higher cap than the full-window walk so find() can reach deep-list
            # items (past the 200th child) — its own max_nodes budget bounds the search.
            for child in list(children)[:_SCOPED_MAX_CHILDREN]:
                if len(results) >= max_results:
                    more[0] = True
                    return
                walk(child, depth + 1, key, child_sib, child_crumbs)

        walk(win, 0, "", {}, [])
        if not results:
            text = (f"(no matches for role={role!r}, name_contains={name_contains!r} in "
                    f"{app_name} — {searched[0]} nodes searched)")
        else:
            lines = [f"=== {app_name} — find(role={role!r}, name_contains={name_contains!r}) "
                     f"— {len(results)} match(es) ==="] + results
            if more[0]:
                lines.append(f"…stopped at {len(results)} matches — narrow with role=/"
                             "name_contains=, or raise max_results")
            text = "\n".join(lines)
        return text, {"est_tokens": round(len(text) / 3.5), "refs": len(self.registry),
                      "app": app_name, "matches": len(results), "searched": searched[0]}

    def _walk(self, el, depth, parent_key, lines, compact, max_depth, budget, sib=None,
              root_key=None, max_children=_MAX_CHILDREN, raw_depth=0):
        if budget["left"] <= 0:
            budget["hit"] = True
            return
        if raw_depth >= _MAX_RECURSION:
            # Every frame counts here, emitted or not — see _MAX_RECURSION.
            lines.append("  " * depth + "…(nesting limit reached — subtree not walked)")
            budget["hit"] = True
            return
        budget["left"] -= 1
        if sib is None:
            sib = {}
        a = ax.get_attrs(el, _ATTRS)
        role, title, desc, value, enabled, name, is_row = self._describe(el, a)
        if root_key is not None and depth == 0:
            # Scoped walk: the root's #occ disambiguator is uncomputable from inside the
            # subtree (its siblings aren't walked) — seed the stored key verbatim so this
            # ref and every descendant ref match the full-walk assignment.
            key = root_key
        else:
            key = self._seg_key(parent_key, sib, role, name, desc)

        # Collapse a selectable row into ONE labelled, selectable line — the fix for
        # "the row is selectable but nameless, its text is an unselectable child."
        if compact and is_row:
            ref = self._ref_for(key)
            self.registry[ref] = el
            lines.append("  " * depth + f"[{ref}] {role}" + (f' "{name}"' if name else ""))
            return

        emit = (not compact) or self._interesting(role, title, value)
        depth_out = depth
        ref = None
        if emit:
            ref = self._ref_for(key)
            self.registry[ref] = el
            lines.append("  " * depth + self._fmt_el(ref, role, title, desc, value, enabled))
            depth_out = depth + 1

        children = a[ax.kAXChildrenAttribute]
        if depth >= max_depth:
            if children:
                lines.append("  " * depth_out + (
                    f"…(+{len(children)} children not walked — snapshot(ref='{ref}') to expand)"
                    if ref else f"…(+{len(children)} children not walked — max depth reached)"))
            return
        if not children:
            return
        child_sib = {}
        for child in list(children)[:max_children]:
            self._walk(child, depth_out, key, lines, compact, max_depth, budget, child_sib,
                       max_children=max_children, raw_depth=raw_depth + 1)
        if len(children) > max_children:
            # Recoverable, like the depth/node caps: give the truncated container a
            # ref (mint one if it wasn't emitted — a list container is usually
            # scaffolding) and point the model at snapshot(ref=...), which re-walks
            # this subtree at the higher scoped child cap.
            trunc_ref = ref if ref is not None else self._ref_for(key)
            self.registry[trunc_ref] = el
            lines.append("  " * depth_out + f"…(+{len(children) - max_children} more of "
                         f"{len(children)} siblings not shown — snapshot(ref='{trunc_ref}') "
                         "to see the rest, or find(name_contains=...) to jump to one)")

    # ── actions ─────────────────────────────────────────────────────────
    def _el(self, ref):
        self._validate_target()
        el = self.registry.get(ref)
        if el is None:
            raise StaleRef(ref)
        return el

    def _center(self, el):
        a = ax.get_attrs(el, (ax.kAXPositionAttribute, ax.kAXSizeAttribute))
        b = ax.values_to_bounds(a[ax.kAXPositionAttribute], a[ax.kAXSizeAttribute])
        if not b:
            return None
        return int(b["x"] + b["w"] / 2), int(b["y"] + b["h"] / 2)

    # press-like actions worth trying when AXPress isn't offered, in order
    _PRESS_ALTERNATIVES = ("AXOpen", "AXConfirm", "AXPick", "AXShowDefaultUI")
    # AXShowDefaultUI is the weakest of those: on a LIST ROW it only expands/collapses the
    # disclosure — it never opens or selects the row, yet it reports success, so a file row in an
    # Open panel "clicks" forever without anything happening. Never offer it for row-ish roles.
    _NO_SHOW_DEFAULT_UI = ("AXRow", "AXCell", "AXOutline", "AXColumn", "AXTable")
    # AXPress on these returns success WITHOUT doing anything — a lie that makes
    # the agent believe it toggled something (observed on System Settings labels)
    _INERT_ROLES = ("AXStaticText", "AXImage")

    @staticmethod
    def _toggle_msg(val):
        """Human + machine-readable checkbox/switch outcome: val 0=off, 1=on."""
        try:
            v = int(val)
        except (TypeError, ValueError):
            return f"toggled to {val}"
        return f"toggled to {v} ({'on' if v else 'off'})"

    def _ax_fire(self, el):
        """Trigger the element through its own AX vocabulary: AXPress, press-like
        alternative actions, then — for checkbox/switch roles — an AXValue flip
        VERIFIED by reading the value back (SwiftUI switches accept the write and
        silently drop it, and also lie about AXPress on their labels). Inert roles
        (labels, images) are never 'pressed' — macOS reports success on them
        without any effect. Returns a message on success, else None."""
        a = ax.get_attrs(el, (ax.kAXRoleAttribute, "AXSubrole", kAXValueAttribute))
        role = str(a[ax.kAXRoleAttribute] or "")
        sub = str(a.get("AXSubrole") or "")
        if role in self._INERT_ROLES:
            return None
        is_toggle = role in ("AXCheckBox", "AXRadioButton") or sub in ("AXSwitch", "AXToggle")
        before_val = None
        if is_toggle:
            try:
                before_val = int(a[kAXValueAttribute])
            except (TypeError, ValueError):
                before_val = None
        if AXUIElementPerformAction(el, "AXPress") == 0:
            if not is_toggle:
                return "pressed"
            # SwiftUI often reports AXPress success while the checkbox stays put —
            # only trust a press when the value actually flipped.
            time.sleep(0.15)
            try:
                after_val = int(ax.get_attr(el, kAXValueAttribute))
            except (TypeError, ValueError):
                after_val = None
            if before_val is not None and after_val is not None and after_val != before_val:
                return self._toggle_msg(after_val)
            # fall through: try alternatives / verified AXValue write
        actions = ax.get_actions(el)
        for name in self._PRESS_ALTERNATIVES:
            if name == "AXShowDefaultUI" and role in self._NO_SHOW_DEFAULT_UI:
                continue
            if name in actions and AXUIElementPerformAction(el, name) == 0:
                if name == "AXShowDefaultUI":
                    return ("performed AXShowDefaultUI — this only REVEALS an element's default "
                            "UI, it is NOT a press, so nothing was opened or chosen — on")
                return f"performed {name}"
        if is_toggle:
            try:
                cur = before_val if before_val is not None else int(a[kAXValueAttribute])
                new = 0 if cur else 1
            except (TypeError, ValueError):
                new = 1
            if AXUIElementSetAttributeValue(el, kAXValueAttribute, new) == 0:
                time.sleep(0.15)
                try:
                    if int(ax.get_attr(el, kAXValueAttribute)) == new:
                        return self._toggle_msg(new)
                except (TypeError, ValueError):
                    pass
        return None

    def _ax_sweep(self, root):
        """_ax_fire over a bounded breadth-first sweep below root."""
        level, seen = [root], 0
        for _ in range(3):
            nxt = []
            for parent in level:
                for c in list(ax.get_attr(parent, ax.kAXChildrenAttribute) or [])[:16]:
                    seen += 1
                    if seen > 48:
                        return None
                    msg = self._ax_fire(c)
                    if msg:
                        return f"{msg} (inner control)"
                    nxt.append(c)
            level = nxt
        return None

    def _ax_activate(self, el):
        """_ax_fire on the element itself; then its descendants (refs often land on
        the wrapper row around the real control); then one parent sweep (refs just
        as often land on the LABEL, whose control is a sibling in the same row).
        One ancestor only — further up, the first control found could be a
        different row's."""
        msg = self._ax_fire(el)
        if msg:
            return msg
        msg = self._ax_sweep(el)
        if msg:
            return msg
        parent = ax.get_attr(el, "AXParent")
        if parent is not None:
            return self._ax_sweep(parent)
        return None

    def _focused_window_title(self):
        """Title of this session's focused/main window, or None if unreadable.
        Used to catch SwiftUI 'AXPress success' lies on sidebar rows (System
        Settings reports pressed while the pane title stays 'Wallpaper')."""
        pid = getattr(self, "_pid", None)
        if pid is None:
            return None
        try:
            win = ax.get_window(AXUIElementCreateApplication(pid))
            if win is None:
                return None
            return str(ax.get_attr(win, ax.kAXTitleAttribute) or "") or None
        except Exception:  # noqa: BLE001 — best-effort signal only
            return None

    def click(self, ref, allow_pixel=True):
        """Activate the element via the AX layer — occlusion-proof and no cursor
        movement: its own press action, a press-like alternative, a checkbox/switch
        value flip, or the real control nested inside the ref. Falls back to a
        coordinate click only if the AX layer offers nothing AND allow_pixel — and
        then only after deliberately raising the app, because a pixel click both
        moves the shared cursor and lands on whichever window is frontmost."""
        el = self._el(ref)
        role = str(ax.get_attr(el, ax.kAXRoleAttribute) or "")
        # A click on an editable text control means "put the insertion point here".
        # Some native sheets advertise AXConfirm on the field itself (notably the
        # Go to Folder sheet). Treating that as a button action leaves keyboard
        # input aimed at the previous control while claiming the click worked.
        # AXFocused is focus-free and, unlike AXConfirm, can be verified.
        if role in ("AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"):
            if AXUIElementSetAttributeValue(el, kAXFocusedAttribute, True) == 0:
                time.sleep(0.1)
                if ax.get_attr(el, kAXFocusedAttribute):
                    return f"focused {ref}"
        # Sidebar/list rows are the main SwiftUI false-success surface: AXPress
        # returns 0 while the pane does not change. Capture the window title so
        # we can refuse to call that a navigation.
        before = self._focused_window_title() if role == "AXRow" else None
        msg = self._ax_activate(el)
        if msg:
            if before is not None:
                time.sleep(0.35)
                after = self._focused_window_title()
                if after == before:
                    # Selection (not press) is what some sidebars actually honor.
                    if AXUIElementSetAttributeValue(el, "AXSelected", True) == 0:
                        time.sleep(0.35)
                        after = self._focused_window_title()
                        if after is not None and after != before:
                            return (f"selected {ref} (navigated {before!r} → {after!r})")
                    return (f"{msg} {ref} — no navigation: window still {before!r}. "
                            f"AXPress reported success but the pane did not change "
                            f"(common in System Settings / SwiftUI). Do not retry the "
                            f"same click — try the View menu, a different control, or "
                            f"select() the row.")
            return f"{msg} {ref}"
        is_toggle = role in ("AXCheckBox", "AXRadioButton")
        if not allow_pixel:
            if is_toggle:
                return (f"{ref}: checkbox/switch did not change via AX (press reported success "
                        f"without a value flip, or AXValue write was dropped — common in SwiftUI "
                        f"System Settings). Re-snapshot: val='0' is OFF, val='1' is ON. If this "
                        f"ref is a label, act on the sibling AXCheckBox. Do NOT use defaults write "
                        f"or AppleScript for System Settings toggles.")
            return f"{ref}: no AX press action; a pixel click would move the shared cursor — skipped"
        c = self._center(el)
        if c is None:
            return f"{ref} not pressable and has no bounds"
        if not self.activate():
            return (f"{ref}: no AX action vocabulary, and couldn't bring "
                    f"'{self._app_name or 'the app'}' to the front for a pixel click — "
                    f"skipped. Try a 'menu' action or a different element.")
        self.disturbances["pixel_clicks"] += 1
        _mouse_click(*c)
        return f"clicked {ref} at {c} (pixel fallback: raised the app, moved the shared cursor)"

    def select(self, ref):
        """Select the element via AX (list rows, table cells, selectable items) —
        occlusion-proof. A general primitive; the agent decides when selection vs
        a press is the right move for the surface it sees.

        The write is VERIFIED by reading AXSelected back: several containers (notably the
        file browser in a native Open/Save panel) accept the write, return success, and stay
        unselected — the panel tracks selection on the PARENT's AXSelectedRows/AXSelectedChildren,
        not the row. An unverified 'selected' there reads as done while the panel's Open button
        stays disabled, with nothing to explain the contradiction."""
        el = self._el(ref)
        if AXUIElementSetAttributeValue(el, "AXSelected", True) != 0:
            return f"{ref} is not selectable"
        time.sleep(0.1)
        if ax.get_attr(el, "AXSelected"):
            return f"selected {ref}"
        # Accepted-then-dropped: try the container, which is what actually owns the selection.
        parent = ax.get_attr(el, "AXParent")
        if parent is not None:
            for attr in ("AXSelectedRows", "AXSelectedChildren"):
                if AXUIElementSetAttributeValue(parent, attr, [el]) == 0:
                    time.sleep(0.1)
                    if ax.get_attr(el, "AXSelected"):
                        return f"selected {ref} (via its container's {attr})"
        return (f"{ref}: the AX select was ACCEPTED BUT DROPPED — it is still not selected, so "
                f"anything gated on the selection (an Open/Choose button) will stay disabled. "
                f"This is typical of a native Open/Save panel. Don't drive that panel: cancel it "
                f"(Escape) and open the path directly with open_file(path, app=...).")

    def right_click(self, ref, allow_pixel=True):
        """Open an element's context menu — AXShowMenu if it exposes one (occlusion-
        proof), else a coordinate right-click (only if allow_pixel — it moves the
        shared cursor). How the agent reaches 'Leave Server', 'Move to Trash', etc."""
        el = self._el(ref)
        if AXUIElementPerformAction(el, "AXShowMenu") == 0:
            return f"opened context menu on {ref}"
        if not allow_pixel:
            return f"{ref}: no AX menu action; a pixel right-click would move the shared cursor — skipped"
        c = self._center(el)
        if c is None:
            return f"{ref} has no bounds"
        if not self.activate():
            return (f"{ref}: no AX menu action, and couldn't bring "
                    f"'{self._app_name or 'the app'}' to the front for a pixel right-click — skipped.")
        self.disturbances["pixel_clicks"] += 1
        _mouse_click(*c, button="right")
        return f"right-clicked {ref} (pixel fallback: raised the app, moved the shared cursor)"

    def set_text(self, ref, text, allow_keystrokes=True):
        """Set a text field's value directly via AX (focus-free). Falls back to real
        keystrokes only if the field isn't AX-settable AND allow_keystrokes (typing
        uses the shared keyboard)."""
        el = self._el(ref)
        # An integrated terminal (Cursor/VS Code) is xterm.js: an AX value-set writes into its
        # screen-reader mirror and NEVER reaches the shell, yet returns 0 (success). Refuse that
        # phantom success and steer to the CDP path, which types real keystrokes into the PTY.
        if _is_editor_terminal(el, getattr(self, "_pid", None)):
            app = self._app_name or "the editor"
            return (f"{ref} is a terminal inside {app} — AX cannot write to it. xterm.js reads "
                    f"keystrokes, not AX values, so an AX set would silently do nothing (it lands "
                    f"in the screen-reader mirror, not the shell). Type into it FOCUS-FREE over "
                    f"CDP instead: web_open(app=\"{app}\") then web_act a 'type' action on the "
                    f"terminal — that injects real keystrokes into the PTY.")
        if AXUIElementSetAttributeValue(el, kAXValueAttribute, text) == 0:
            if str(ax.get_attr(el, kAXValueAttribute)) == str(text):
                return f"set text on {ref} (readback verified)"
            return f"{ref}: AX accepted the write but readback did not match; result unverified"
        if not allow_keystrokes:
            return f"{ref}: field not AX-settable; typing would use the shared keyboard — skipped"
        if not self.activate():
            return (f"{ref}: field not AX-settable, and couldn't bring "
                    f"'{self._app_name or 'the app'}' to the front — keystrokes land on the "
                    f"frontmost app, so typing blind was skipped.")
        AXUIElementSetAttributeValue(el, kAXFocusedAttribute, True)
        self.disturbances["keystrokes"] += 1
        _type_text(text)
        return f"typed into {ref} (keystroke fallback: raised the app, used the shared keyboard)"

    def type_text(self, text):
        self.disturbances["keystrokes"] += 1
        _type_text(text)
        return f"typed {len(text)} chars"

    def press_key(self, key, modifiers=None):
        self.disturbances["key_combos"] += 1
        _press_key(key, modifiers or [])
        return f"key {'+'.join((modifiers or []) + [key])}"

    def set_window(self, x=None, y=None, w=None, h=None, app_name=None):
        """Move/resize the target app's MAIN window via AX — focus-free, no cursor.
        Targets AXMainWindow (via get_window), NOT 'window 1' by index: a modal SHEET
        (a save panel, a locked-note password prompt) can BE window 1, and a blind
        index resize hits the sheet instead. Sets only the axes given, reads the
        result back, and reports the ACTUAL geometry (never assumes the set stuck —
        some windows clamp to a min size or refuse)."""
        # Resolve the target. An explicit app_name that differs from the current target
        # re-resolves (so ONE act batch can tile SEVERAL apps — e.g. TextEdit left, Notes
        # right); otherwise use the session's app, establishing it on the first window-op
        # when nothing has been snapshotted yet. A named app resolves to a LOCAL pid so a
        # one-off window op on another app never clobbers the session's main target.
        pid, name = self._pid, self._app_name
        if app_name and (not pid or (name or "").lower() != app_name.lower()):
            match = _resolve_app(app_name)   # window ops need no ref, so may run before any snapshot
            if not match:
                return f"no running app named {app_name!r} to move/resize — check it's running"
            pid, name = match["pid"], match["name"]
            if not self._pid:                # first target also becomes the session's app
                self._pid, self._app_name = pid, name
        if not pid:
            return "no target app for window op — snapshot the app first, or check it's running"
        ax_app = AXUIElementCreateApplication(pid)
        win = ax.get_window(ax_app)
        if win is None:
            return f"{name or 'app'} exposes no window to move/resize"
        # refuse to act on a sheet-obscured window: the geometry the user means is
        # the main window's, and a sheet must be dismissed first
        sheet = ax.get_attr(win, "AXSubrole")
        if x is not None or y is not None:
            cur = ax.get_attr(win, ax.kAXPositionAttribute)
            b = ax.values_to_bounds(cur, ax.get_attr(win, ax.kAXSizeAttribute)) or {}
            nx = x if x is not None else int(b.get("x", 0))
            ny = y if y is not None else int(b.get("y", 0))
            AXUIElementSetAttributeValue(win, ax.kAXPositionAttribute,
                                         AXValueCreate(kAXValueCGPointType, CGPoint(nx, ny)))
        if w is not None or h is not None:
            cur = ax.values_to_bounds(ax.get_attr(win, ax.kAXPositionAttribute),
                                      ax.get_attr(win, ax.kAXSizeAttribute)) or {}
            nw = w if w is not None else int(cur.get("w", 0))
            nh = h if h is not None else int(cur.get("h", 0))
            AXUIElementSetAttributeValue(win, ax.kAXSizeAttribute,
                                         AXValueCreate(kAXValueCGSizeType, CGSize(nw, nh)))
        got = ax.values_to_bounds(ax.get_attr(win, ax.kAXPositionAttribute),
                                  ax.get_attr(win, ax.kAXSizeAttribute)) or {}
        note = " (a sheet is attached — dismiss it first if this looks wrong)" if sheet == "AXSheet" else ""
        return (f"{name}: window now at ({got.get('x')},{got.get('y')}) "
                f"{got.get('w')}×{got.get('h')}{note}")

    def menu(self, path):
        """Invoke a menu-bar command by path, e.g. ["File", "Move to Trash"], via AXPress —
        FOCUS-FREE: it runs the command WITHOUT bringing the app to the front. This is the
        focus-free stand-in for keyboard shortcuts (⌘⌫ move-to-trash, ⌘S save, ⌘W close, ⌘N
        new, ...). Prefer this over a `key` action whenever the command lives in a menu."""
        if not self._pid:
            return "no target app for menu"
        if not path:
            return "menu needs a path like [\"File\", \"Move to Trash\"]"
        ax_app = AXUIElementCreateApplication(self._pid)
        mb = ax.get_attr(ax_app, ax.kAXMenuBarAttribute)
        if mb is None:
            return f"{self._app_name or 'app'} exposes no menu bar"
        items = ax.get_attr(mb, ax.kAXChildrenAttribute) or []
        def title(c):
            return str(ax.get_attr(c, ax.kAXTitleAttribute) or "").strip()
        for depth, name in enumerate(path):
            match = (next((c for c in items if title(c) == name), None)
                     or next((c for c in items if name.lower() in title(c).lower()), None))
            if match is None:
                where = " > ".join(path[:depth]) or "menu bar"
                return (f"menu item {name!r} not found under {where}; "
                        f"available: {[title(c) for c in items if title(c)][:25]}")
            if depth == len(path) - 1:
                rc = AXUIElementPerformAction(match, "AXPress")
                return (f"invoked menu {' > '.join(path)}" if rc == 0 else
                        f"menu {' > '.join(path)} found but AXPress failed ({rc}) — item may be disabled")
            sub = (ax.get_attr(match, ax.kAXChildrenAttribute) or [None])[0]
            items = (ax.get_attr(sub, ax.kAXChildrenAttribute) or []) if sub is not None else []
        return "empty menu path"


# ── CGEvent input helpers ────────────────────────────────────────────────
def _mouse_click(x, y, button="left"):
    down = Quartz.kCGEventLeftMouseDown if button == "left" else Quartz.kCGEventRightMouseDown
    up = Quartz.kCGEventLeftMouseUp if button == "left" else Quartz.kCGEventRightMouseUp
    btn = Quartz.kCGMouseButtonLeft if button == "left" else Quartz.kCGMouseButtonRight
    ev = Quartz.CGEventCreateMouseEvent(None, down, (x, y), btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    ev = Quartz.CGEventCreateMouseEvent(None, up, (x, y), btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _mouse_drag(x1, y1, x2, y2, steps=12):
    """Press-move-release from (x1,y1) to (x2,y2). Intermediate dragged events matter —
    many targets (canvas drag-and-drop, sliders, reorder lists) ignore a down/up with no
    motion between. Coordinates are POINTS (same space as click_xy and the point-scaled
    screenshot)."""
    L = Quartz.kCGMouseButtonLeft
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x1, y1), L))
    for i in range(1, steps + 1):
        x = x1 + (x2 - x1) * i / steps
        y = y1 + (y2 - y1) * i / steps
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                           Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, (x, y), L))
        time.sleep(0.012)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x2, y2), L))


def _type_text(text):
    """Type arbitrary text by attaching the unicode string to a synthetic key
    event — no per-character keycode mapping needed."""
    ev = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
    Quartz.CGEventKeyboardSetUnicodeString(ev, len(text), text)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
    Quartz.CGEventKeyboardSetUnicodeString(up, len(text), text)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


_KEYCODES = {"return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
             "backspace": 51,
             "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126,
             # US-QWERTY letters/digits, so ⌘-shortcuts (⌘Q, ⌘C, ⌘W, ...) work
             "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
             "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
             "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45,
             "m": 46, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
             "8": 28, "9": 25, "0": 29}
_MODFLAGS = {"command": Quartz.kCGEventFlagMaskCommand, "cmd": Quartz.kCGEventFlagMaskCommand,
             "shift": Quartz.kCGEventFlagMaskShift, "option": Quartz.kCGEventFlagMaskAlternate,
             "control": Quartz.kCGEventFlagMaskControl, "ctrl": Quartz.kCGEventFlagMaskControl}


def _press_key(key, modifiers):
    code = _KEYCODES.get(key.lower())
    if code is None:
        _type_text(key)
        return
    flags = 0
    for m in modifiers:
        flags |= _MODFLAGS.get(m.lower(), 0)
    down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
    if flags:
        Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
    if flags:
        Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _main_display_points():
    """(width, height, scale) of the main display in POINTS — the coordinate space
    click_xy/CGEvent uses. Returns (None, None, 1.0) if AppKit is unavailable."""
    try:
        from AppKit import NSScreen
        scr = NSScreen.mainScreen()
        fr = scr.frame().size
        return int(fr.width), int(fr.height), float(scr.backingScaleFactor())
    except Exception:
        return None, None, 1.0


def screenshot_b64():
    """PNG of the main display, base64. DOWNSCALED to POINTS so a coordinate the model
    reads off the image maps 1:1 to click_xy: `screencapture` grabs NATIVE pixels, which
    on a Retina display is 2x the point space CGEvent clicks in — un-scaled, every
    vision click lands ~2x too far. sips resizes to points (no-op on a 1x display)."""
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        try:
            subprocess.run(["screencapture", "-x", "-t", "png", f.name], check=True)
        except subprocess.CalledProcessError:
            raise RuntimeError(
                "screencapture failed — almost always the Screen Recording permission missing "
                "for the app hosting Hunch. Tell the user: System Settings → Privacy & Security "
                "→ Screen Recording, enable the MCP host app (toggle off/on if already listed), "
                "then restart it. Meanwhile, prefer `snapshot` — it reads UI without this "
                "permission.")
        w, h, scale = _main_display_points()
        if w and h and scale and scale != 1.0:
            # native (2x on Retina) -> points, so image coords == click_xy coords. `sips -z`
            # takes height then width. Best-effort: if sips fails, return the native shot.
            subprocess.run(["sips", "-z", str(h), str(w), f.name],
                           check=False, capture_output=True)
        return base64.b64encode(open(f.name, "rb").read()).decode()


# ── App lifecycle — OS-backed, reliable, NOT UI-driven ───────────────────────
# Use these for launch/quit/focus/list. They sidestep the focus limitation that
# makes ⌘Q / Dock-clicking flaky on background apps. Use the TREE primitives
# (snapshot/act) for work INSIDE an app; use these to manage the apps themselves.
def list_running_apps():
    return ", ".join(sorted(a["name"] for a in ax.list_apps()))


def launch_app(name, force_accessibility=False, background=False, debugging_port=None):
    """Launch explicitly; a forced-accessibility restart must quit gracefully."""
    bg = ["-g"] if background else []
    match = _resolve_app(name)
    identity = _running_identity(match["pid"]) if match else {}
    target = identity.get("path") or name
    profile_args = []
    if (force_accessibility or debugging_port) and match and "--user-data-dir" in _proc_cmdline(match["pid"]):
        from .cdp import _OWNED_ENDPOINTS
        from .targets import process_key
        owned = next((record for record in _OWNED_ENDPOINTS.values()
                      if process_key(record["identity"]) == process_key(identity)), None)
        if owned is None:
            return "REFUSED: current custom profile is not owned/verified; attach to its endpoint without restarting"
        profile_args = [f"--user-data-dir={owned['profile']}"]
    if (force_accessibility or debugging_port) and match:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(match["pid"])
        if app is None:
            return f"REFUSED: selected process for {name} disappeared; resolve it again"
        app.terminate()
        deadline = time.monotonic() + 6
        while _process_alive(match["pid"]) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _process_alive(match["pid"]):
            return f"REFUSED: {name} did not quit gracefully; resolve its save/confirmation prompt"
    if not background:
        _announce_front(name)
    args = ["open", *bg, "-a", target]
    if force_accessibility:
        args += ["--args", _FORCE_AX_FLAG, *profile_args]
    elif debugging_port:
        args += ["--args", f"--remote-debugging-port={int(debugging_port)}",
                 "--remote-debugging-address=127.0.0.1", *profile_args]
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode:
        return f"failed to launch {name}: {result.stderr.strip() or result.returncode}"
    if debugging_port:
        from .cdp import _wait_for_port, verify_endpoint
        _wait_for_port(debugging_port, timeout=15)
        verify_endpoint(debugging_port, target)
        return f"debugging endpoint verified for {target} on :{debugging_port}"
    if force_accessibility:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            current = _resolve_app(target)
            if current and _proc_has_force_ax(current["pid"]):
                return f"launched {name} with accessibility flag; operation coverage remains unverified"
            time.sleep(0.3)
        return f"REFUSED: launch requested for {name}, but the accessibility flag was not verified"
    return f"launch requested for {name}" + (" in background" if background else "")


def _activate_process(pid):
    """Exact-process fallback when cooperative AppKit activation is refused.

    Callers must pass the shared-input gate first and verify foreground afterward.
    """
    try:
        result = subprocess.run([
            "osascript", "-e", 'tell application "System Events" to set frontmost of '
            f'first application process whose unix id is {int(pid)} to true'],
            capture_output=True, text=True, timeout=3)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def focus_app(name):
    """Activate the resolved process and verify the foreground PID."""
    target = _resolve_app(name)
    if target is None:
        return f"REFUSED: {name} is not running; launch it explicitly first"
    _announce_front(name)
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(target["pid"])
    if (app is None or not app.activateWithOptions_(2)) and not _activate_process(target["pid"]):
        return f"REFUSED: activation request failed for {name}"
    for _ in range(20):
        if _frontmost()[1] == target["pid"]:
            return f"focused {name} (pid {target['pid']})"
        time.sleep(0.1)
    return f"UNVERIFIED: activation requested for {name}, but foreground PID did not match"


def quit_app(name):
    """Quit an app cleanly via the OS — reliable regardless of focus or tray
    behavior, unlike ⌘Q. Polls until it's actually gone; never force-quits."""
    r = _resolve_app(name)
    if r is None:
        return f"{name} is not running"
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(r["pid"])
    app.terminate()
    # Poll the app object's own termination flag — the NSWorkspace running-apps
    # list lags behind an actual quit, so checking it gives false "still running".
    for i in range(20):  # up to ~6s; never force-quit implicitly
        time.sleep(0.3)
        if not _process_alive(r["pid"]):
            return f"quit {name}"
    return f"{name} still running (it may be showing a save/confirm prompt)"


# ── LocalComputer: the tool-use adapter (same interface as Docker computer.py) ──
from .tool_registry import BASE_TOOLS
TOOLS = [tool for tool in BASE_TOOLS if tool["name"] in {
    "snapshot", "act", "screenshot", "list_apps", "launch_app", "quit_app", "focus_app"}]


_REF_LINE = re.compile(r"\[(e\d+)\]")


def _snapshot_delta(prev, cur):
    """Delta view of a fresh snapshot against the previous one, keyed by stable refs
    (the _keymap gives the same eN to the same element across snapshots). Returns the
    delta text, or None when a full tree is the honest answer: first view, the window
    itself changed, or most lines changed anyway. '(no visible change)' is a valid delta.
    Emission stays in current-tree order; unchanged lines are omitted — the point:
    act() was re-sending the entire tree after every action (9 full System Settings
    trees in one bench run), and the unchanged 95% is pure token cost."""
    if not prev:
        return None
    pl, cl = prev.splitlines(), cur.splitlines()
    def refmap(lines):
        m = {}
        for ln in lines:
            r = _REF_LINE.search(ln)
            if r:
                m.setdefault(r.group(1), ln)
        return m
    pm, cm = refmap(pl), refmap(cl)
    if not pm or not cm:
        return None
    def window_line(lines):
        return next((ln for ln in lines if "AXWindow" in ln), None)
    if window_line(pl) != window_line(cl):
        return None   # different window (or retitled) — show the full tree
    out, changed_n = [], 0
    for ln in cl:
        r = _REF_LINE.search(ln)
        if not r:
            continue
        ref = r.group(1)
        if ref not in pm:
            out.append("+ " + ln.strip())
            changed_n += 1
        elif pm[ref] != ln:
            out.append("~ " + ln.strip())
            changed_n += 1
    gone = [r for r in pm if r not in cm]
    changed_n += len(gone)
    if changed_n == 0:
        return "(no visible change since the last view)"
    if changed_n > 0.5 * len(cm):
        return None   # the screen mostly changed — a delta would be noise
    if gone:
        out.append("gone: " + ", ".join(sorted(gone, key=lambda x: int(x[1:]))[:40]))
    return "\n".join(out)


class LocalComputer:
    """Drives the real Mac through MacSession, exposing the same tools/handle
    contract the agent loop expects. Swap this in for the Docker Computer."""

    def __init__(self, app="Finder", simultaneous=False,
                 max_depth=None, max_nodes=None):
        self.app = app
        # simultaneous=True: never steal the user's cursor/keyboard. Runs the
        # focus-free primitives (AXPress/select/set_value) without bringing the app
        # forward, and REFUSES the shared-input ones (typed keystrokes, key combos,
        # pixel clicks) instead of disrupting whatever the user is doing.
        self.simultaneous = simultaneous
        self.session = MacSession()
        self.max_depth = max_depth   # instance defaults for full snapshots (None -> module defaults)
        self.max_nodes = max_nodes
        self.tools = TOOLS
        self._last_snap = None   # baseline for act()'s delta view

    def snapshot(self, ref=None, max_depth=None, max_nodes=None, max_children=None):
        text, _ = self.session.snapshot(app_name=self.app, compact=True,
                                        activate_app=not self.simultaneous,
                                        ref=ref,
                                        max_depth=max_depth if max_depth is not None else self.max_depth,
                                        max_nodes=max_nodes if max_nodes is not None else self.max_nodes,
                                        max_children=max_children)
        if ref is None:
            self._last_snap = text   # full-window views re-baseline the delta; subtree views don't
        return text

    def find(self, role=None, name_contains=None, max_results=20):
        text, _ = self.session.find(role=role, name_contains=name_contains,
                                    max_results=max_results, app_name=self.app)
        return text

    @staticmethod
    def _is_shared_input(a):
        """Actions that use the ONE shared cursor/keyboard (collide with the user)."""
        act = a.get("action")
        return act in ("key", "click_xy", "drag") or (act == "type" and not a.get("ref"))

    def _endpoint(self, a, side):
        """Resolve a drag endpoint: {side}_ref -> that element's center, else
        ({side}_x, {side}_y) as point coords. Returns (x, y) or None."""
        ref = a.get(f"{side}_ref")
        if ref:
            return self.session._center(self.session._el(ref))
        x, y = a.get(f"{side}_x"), a.get(f"{side}_y")
        return (int(x), int(y)) if x is not None and y is not None else None

    @staticmethod
    def _coerce_actions(actions):
        """Normalize `actions` to a list of dicts. Small local models (and some
        providers) occasionally pass a JSON *string* instead of an array — iterating
        that string made every char hit `.get` and raised
        `'str' object has no attribute 'get'` (seen 2026-08-08 on qwen3.5:9b)."""
        if isinstance(actions, str):
            try:
                actions = json.loads(actions)
            except (json.JSONDecodeError, TypeError):
                return None, ("actions must be an array of objects like "
                              '[{"action":"click","ref":"e12"}], not a string — '
                              "got a JSON string that doesn't parse. Re-send as a "
                              "real array, not a stringified one.")
        if not isinstance(actions, list):
            return None, (f"actions must be an array of objects, got {type(actions).__name__}")
        if any(not isinstance(a, dict) for a in actions):
            return None, ("actions must be an array of objects like "
                          '[{"action":"click","ref":"e12"}] — each item must be an object')
        return actions, None

    def act(self, actions, detailed=False, postcondition=None):
        from .results import action_receipt, validate_postcondition
        validate_postcondition(postcondition)
        actions, bad = self._coerce_actions(actions)
        if bad:
            return bad
        sim = self.simultaneous
        _dist_before = dict(self.session.disturbances)
        # Only bring the app forward if the batch actually contains a focus-stealing action.
        # Focus-free primitives (click / select / right_click / set-field / menu) never
        # activate the app — so they never disturb the user's foreground.
        needs_focus = any(self._is_shared_input(a) for a in actions)
        front_ok = True if (sim or not needs_focus) else self.session.activate()
        lines = []
        for a in actions:
            act = a.get("action")
            try:
                if sim and self._is_shared_input(a):
                    lines.append(f"skipped '{act}': needs the shared keyboard/cursor — refused in "
                                 f"simultaneous mode so it can't disrupt you. Use a focus-free "
                                 f"alternative: a 'menu' action (e.g. File > Move to Trash), or "
                                 f"click/select/right_click by ref.")
                    break
                if self._is_shared_input(a) and not sim and not front_ok:
                    lines.append(f"refused '{act}': couldn't bring '{self.app}' to the front "
                                 f"(modern macOS blocks focus-stealing, especially right after a "
                                 f"dialog). Use a focus-free alternative: a 'menu' action, or "
                                 f"click/select/right_click by ref.")
                    break
                if act == "click":
                    lines.append(self.session.click(a["ref"], allow_pixel=not sim))
                elif act == "right_click":
                    lines.append(self.session.right_click(a["ref"], allow_pixel=not sim))
                elif act == "select":
                    lines.append(self.session.select(a["ref"]))
                elif act == "menu":
                    lines.append(self.session.menu(a.get("path") or []))
                elif act == "type":
                    lines.append(self.session.set_text(a["ref"], a.get("text", ""), allow_keystrokes=not sim)
                                 if a.get("ref") else self.session.type_text(a.get("text", "")))
                elif act == "key":
                    lines.append(self.session.press_key(a["key"], a.get("modifiers")))
                elif act == "window":
                    lines.append(self.session.set_window(
                        x=a.get("x"), y=a.get("y"), w=a.get("w"), h=a.get("h"),
                        app_name=a.get("app") or self.app))
                elif act == "drag":
                    fp, tp = self._endpoint(a, "from"), self._endpoint(a, "to")
                    if not fp or not tp:
                        lines.append("drag needs a from and a to point: from_ref/to_ref (element "
                                     "centers) or from_x/from_y/to_x/to_y (point coords)")
                    else:
                        self.session.disturbances["drags"] += 1
                        _mouse_drag(fp[0], fp[1], tp[0], tp[1])
                        lines.append(f"dragged {fp} -> {tp}")
                elif act == "click_xy":
                    self.session.disturbances["pixel_clicks"] += 1
                    _mouse_click(int(a["x"]), int(a["y"]))
                    lines.append(f"clicked ({a['x']},{a['y']})")
                else:
                    lines.append(f"unknown action {act}")
                if lines and any(marker in lines[-1].lower() for marker in ("refused", "skipped", "unverified", "not selectable")):
                    break
                time.sleep(0.6)
            except StaleRef:
                lines.append(f"ref {a.get('ref')} is stale — re-snapshot")
                break
            except Exception as e:  # noqa: BLE001
                lines.append(f"error on {act}: {e}")
                break
        time.sleep(0.8)
        d = self.session.disturbances
        delta = {k: d[k] - _dist_before[k] for k in d if d[k] > _dist_before[k]}
        outcome = None
        if detailed or postcondition is not None:
            def read(ref, field):
                attributes = {"value": "AXValue", "title": "AXTitle", "enabled": "AXEnabled",
                              "selected": "AXSelected", "expanded": "AXExpanded"}
                element = self.session._el(ref)
                error, value = ax.read_attr(element, attributes[field])
                if error:
                    raise ValueError(f"AX readback failed ({error})")
                return value
            outcome = action_receipt(lines, len(actions), "ax", getattr(self.session, "_identity", None),
                                     delta, postcondition, read)
        receipt = ""
        if delta:
            used = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in delta.items())
            total = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in d.items() if v)
            receipt = (f"\n\nShared-screen use this call: {used} (session total: {total}). "
                       f"Prefer focus-free primitives where possible.")
        prev = self._last_snap
        shot = self.snapshot()   # fresh full tree; also re-baselines
        diff = _snapshot_delta(prev, shot)
        screen = ("Screen changes since your last view (~ changed, + new; unchanged lines "
                  "omitted — call snapshot for the full tree):\n" + diff
                  ) if diff is not None else "Screen now:\n" + shot
        if outcome is not None:
            return {**outcome, "observation": screen}
        return "Executed:\n" + "\n".join(lines) + receipt + "\n\n" + screen

    def handle(self, tool_use):
        name = tool_use.name
        args = tool_use.input
        try:
            if name == "screenshot":
                return {"type": "tool_result", "tool_use_id": tool_use.id, "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": screenshot_b64()}}]}
            if name == "snapshot":
                content = self.snapshot()
            elif name == "act":
                content = self.act(args["actions"], detailed=args.get("detailed", False),
                                   postcondition=args.get("postcondition"))
            elif name == "list_apps":
                content = list_running_apps()
            elif name == "launch_app":
                # In simultaneous mode, launch in the background so it never steals the user's view.
                content = launch_app(args["name"], args.get("force_accessibility", False),
                                     background=self.simultaneous)
                self.app = args["name"]  # target the newly-launched app for subsequent snapshots
            elif name == "quit_app":
                content = quit_app(args["name"])
            elif name == "focus_app":
                content = focus_app(args["name"])
                self.app = args["name"]
            else:
                return {"type": "tool_result", "tool_use_id": tool_use.id,
                        "content": f"unknown tool {name}", "is_error": True}
            return {"type": "tool_result", "tool_use_id": tool_use.id, "content": content}
        except Exception as e:  # noqa: BLE001
            return {"type": "tool_result", "tool_use_id": tool_use.id,
                    "content": f"error: {e}", "is_error": True}
