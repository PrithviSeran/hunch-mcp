"""sdk.py — Hunch as an importable Python library: deterministic, focus-free Mac automation.

    from hunch import Hunch

    mac = Hunch()                          # your own machine — no sandbox, your logged-in apps
    print(mac.snapshot("Mail"))            # accessibility tree, focus-free
    mac.act([{"action": "click", "ref": "e12"}])
    mac.web.open(url="https://github.com") # real Chrome profile over CDP
    mac.files.trash(["~/Downloads/old.zip"])
    mac.applescript('tell application "Music" to play')

Same primitives as the MCP tools, same safety layer (gate.py), no LLM in the loop.
The consent gates default ON: focus switches and risky actions pop the one-click
'Go ahead' dialog governed by ~/.hunch/config.json, exactly like the MCP server.
Pass confirm="off" to auto-approve for this instance only (unattended scripts —
you accept the risk; the config file and HUNCH_NO_INTERNAL_GATE are untouched).

Permissions: unlike the MCP server (where the host app — Claude Desktop, etc. —
holds the grants), here it's WHATEVER RUNS YOUR SCRIPT (your terminal or IDE) that
needs Accessibility (System Settings → Privacy & Security → Accessibility), and
Screen Recording if you call screenshot(). The constructor checks Accessibility
up front and raises AccessibilityNotGranted with instructions.

Error model: methods return status strings (check for "REFUSED"/"couldn't"), and
raise only where flow control demands it — ApprovalDenied when the user declines a
consent dialog, WebNotOpen for .web calls before .web.open(), StaleRef when an
element ref expired (re-snapshot). fill_login/fill_secret NEVER return or store
credential values; they go from the Keychain straight into the page.

Process-wide caveat: the focus-notice suppression window in local_mac is module
state, so multiple Hunch instances in one process share it. One process drives
one Mac, so in practice this doesn't bite.
"""
import base64

from . import gate
from . import os_ops
from .gate import (HunchError, ApprovalDenied, AccessibilityNotGranted, WebNotOpen)  # re-export
from .cli import CDP_PORT
from .local_mac import (LocalComputer, StaleRef, screenshot_b64, list_running_apps,
                        set_focus_reason,
                        launch_app as _launch_app, quit_app as _quit_app, focus_app as _focus_app)
from .notify import notify as _notify

__all__ = ["Hunch", "HunchError", "ApprovalDenied", "AccessibilityNotGranted",
           "WebNotOpen", "StaleRef"]


def _frontmost():
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.localizedName() if app else "Finder"


class Hunch:
    """A deterministic client for this Mac. See the module docstring for the contract."""

    def __init__(self, app="Finder", confirm="dialog", check_permissions=True,
                 simultaneous=False, cdp_port=None, walk_workers=None,
                 snapshot_max_depth=None, snapshot_max_nodes=None,
                 agent_backend="auto"):
        """agent_backend: which LLM transport `mac.agent` uses — 'api' (anthropic SDK on a
        metered ANTHROPIC_API_KEY), 'subscription' (claude-agent-sdk on the `hunch login`
        Claude sign-in), or 'auto' (pick by credentials). Overridable per call via
        mac.agent.run(..., backend=...)."""
        self._gate = gate.Gate(confirm=confirm)   # validates confirm
        if agent_backend not in ("auto", "api", "subscription"):
            raise HunchError(f"unknown agent_backend {agent_backend!r} — "
                             "use 'api', 'subscription', or 'auto'")
        if check_permissions:
            self._check_accessibility()
        # ONE persistent computer per instance, so element [refs] survive snapshot -> act.
        self._computer = LocalComputer(app=app, simultaneous=simultaneous,
                                       walk_workers=walk_workers,
                                       max_depth=snapshot_max_depth,
                                       max_nodes=snapshot_max_nodes)
        self.web = Web(self, cdp_port or CDP_PORT)
        self.files = Files(self)
        self.clipboard = Clipboard(self)
        self._agent = None   # the agent loop, created lazily so the LLM SDKs stay optional
        self._agent_backend = agent_backend

    @staticmethod
    def _check_accessibility():
        try:
            from ApplicationServices import AXIsProcessTrusted
        except ImportError as e:
            raise HunchError(
                "pyobjc is missing — install the full package: pip install hunch-sdk") from e
        if not AXIsProcessTrusted():
            raise AccessibilityNotGranted(
                "this process is not trusted for Accessibility, so tree reads and UI actions "
                "would all come back empty. Grant the app running this script (your terminal "
                "or IDE) in System Settings → Privacy & Security → Accessibility, then "
                "restart it. Pass check_permissions=False to skip this check (e.g. for "
                "files/clipboard/web-only use).")

    # ── native apps: AX tree ──────────────────────────────────────────────────

    def snapshot(self, app="", ref=None, max_depth=None, max_nodes=None):
        """The app's focused window as a ref-annotated accessibility tree (focus-free).
        Empty `app` targets the frontmost app. Pass ref="e42" to expand ONLY that
        element's subtree at full depth (other refs stay valid). Truncation is never
        silent: capped output ends in an explicit …marker naming the ref to expand."""
        if ref is not None:
            return self._computer.snapshot(ref=ref, max_depth=max_depth, max_nodes=max_nodes)
        prev = self._computer.app
        self._computer.app = app or _frontmost()
        out = self._computer.snapshot(max_depth=max_depth, max_nodes=max_nodes)
        if isinstance(out, str) and out.startswith("(app '") and "not found" in out:
            self._computer.app = prev   # a failed target must not poison later calls
        return out

    def find(self, role=None, name_contains=None, app="", max_results=20):
        """Search an app's WHOLE accessibility tree (deeper than snapshot shows) and
        return only matching elements, each with an actable [ref]. role is
        case-insensitive with the AX prefix optional ("button" == AXButton);
        name_contains matches title/description/value as a substring."""
        prev = self._computer.app
        if app:
            self._computer.app = app
        out = self._computer.find(role=role, name_contains=name_contains, max_results=max_results)
        if isinstance(out, str) and out.startswith("(app '") and "not found" in out:
            self._computer.app = prev
        return out

    def act(self, actions, reason=""):
        """Run UI actions (same dicts as the MCP `act` tool: click/select/right_click/type/
        menu/key/click_xy by ref) and return the updated tree. Focus-stealing actions are
        gated; a user refusal raises ApprovalDenied. StaleRef means re-snapshot."""
        blocked = gate.check_focus_steal(self._computer, actions, self._gate,
                                         confirm=False, reason=reason)
        if blocked:
            raise ApprovalDenied(blocked)
        return self._computer.act(actions)

    def screenshot(self):
        """The physical screen as PNG bytes (needs Screen Recording permission). Shows the
        FRONTMOST app — for a background CDP page use web.screenshot() instead."""
        return base64.b64decode(screenshot_b64())

    # ── app lifecycle ─────────────────────────────────────────────────────────

    def list_apps(self):
        """Names of the running GUI apps you can snapshot()."""
        return list_running_apps()

    def launch_app(self, name, force_accessibility=False, reason=""):
        """Launch or focus an app and target it for snapshots. force_accessibility=True
        relaunches an Electron/Chromium app so its tree becomes readable. A foreground
        launch is a real focus switch: gated — refusal raises ApprovalDenied."""
        if not self._computer.simultaneous:   # foreground launch = a real focus switch
            blocked = self._gate.front_gate(name, reason)
            if blocked:
                raise ApprovalDenied(blocked)
        set_focus_reason(reason)
        msg = _launch_app(name, force_accessibility, background=self._computer.simultaneous)
        self._computer.app = name
        return msg

    def quit_app(self, name):
        return _quit_app(name)

    def focus_app(self, name, reason=""):
        """Bring an app to the front and target it. Gated — refusal raises ApprovalDenied."""
        blocked = self._gate.front_gate(name, reason)
        if blocked:
            raise ApprovalDenied(blocked)
        set_focus_reason(reason)
        msg = _focus_app(name)
        self._computer.app = name
        return msg

    @property
    def simultaneous(self):
        """When True, Hunch never touches the foreground/cursor/keyboard: background
        launches, focus-free actions only (shared-input actions are refused)."""
        return self._computer.simultaneous

    @simultaneous.setter
    def simultaneous(self, on):
        self._computer.simultaneous = bool(on)

    # ── AppleScript / OS ──────────────────────────────────────────────────────

    def applescript(self, script):
        """Run AppleScript against scriptable apps (Mail, Music, Finder, …) focus-free.
        Mutating/'do shell script' scripts are gated; refusal raises ApprovalDenied."""
        category = gate.applescript_category(script)
        if category and self._gate.enabled(category):
            preview = script.strip()[:400].replace("\n", "  ").replace("\r", " ")
            if not self._gate.confirm_dialog(
                    "Hunch wants to run an AppleScript that can change things or "
                    f"control apps:  {preview}   — allow?", screen_approval=False):
                raise ApprovalDenied("user did not approve the AppleScript")
        ok, out = os_ops.run_applescript(script)
        if ok:
            return out
        return f"AppleScript error: {out[:600]}{gate.applescript_hint(out)}"

    def notify(self, message, title="Hunch"):
        """Best-effort macOS desktop notification."""
        _notify(message, title, sound="Ping")

    def list_credentials(self):
        """Names + kinds of saved credentials (never any values). Fill them into a CDP
        page with web.fill_login(service) / web.fill_secret(service, ref)."""
        from .creds import list_services, kind_of
        names = list_services()
        if not names:
            return "No saved credentials. Add them with: hunch creds add <service>"
        logins = [n for n in names if kind_of(n) != "secret"]
        secrets = [n for n in names if kind_of(n) == "secret"]
        parts = []
        if logins:
            parts.append("logins (web.fill_login): " + ", ".join(logins))
        if secrets:
            parts.append("API keys/secrets (web.fill_secret): " + ", ".join(secrets))
        return "Saved credentials — " + "; ".join(parts)

    # ── agent loop ────────────────────────────────────────────────────

    @property
    def agent(self):
        """The agent loop: `mac.agent.run(task=...)` runs an LLM loop that drives this Mac
        through these primitives. Two backends (pip install 'hunch-sdk[agent]' brings both):
        your Claude subscription after `hunch login`, or a metered ANTHROPIC_API_KEY.
        Chosen at construction via Hunch(agent_backend=...), by credentials on 'auto', or
        per call via run(backend=...). Created lazily so plain use imports neither SDK."""
        if self._agent is None:
            from .agent import Agent
            self._agent = Agent(self, backend=self._agent_backend)
        return self._agent

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def close(self):
        """Close the CDP web session, if any (the browser window stays open), and stop
        the agent loop's subscription backend if it was started."""
        self.web.close()
        if self._agent is not None:
            self._agent.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Web:
    """Focus-free browser/Electron control over CDP, on the persistent Hunch profile.
    Shares the CDP port with the MCP server, so whichever opened the instance first is
    gracefully reused — but restart()/login() kill whatever holds the port."""

    def __init__(self, hunch, port):
        self._h = hunch
        self.port = port
        self._computer = None   # per-instance CDPComputer (the server's _cdp equivalent)

    def _session(self):
        if self._computer is None:
            raise WebNotOpen("no web/Electron app open — call web.open() first")
        return self._computer.session

    def open(self, url="", app="Google Chrome", isolated=False):
        """Open a Chromium browser (or Electron app) for focus-free control. Uses the
        persistent, dedicated Hunch profile (isolated=True: throwaway sandbox profile).
        If the profile isn't signed into the site, the returned string says so — call
        login() once, then reopen."""
        import os as _os
        if _os.environ.get("HUNCH_FORCE_SANDBOX") == "1":
            isolated = True
        from .cdp import CDPComputer
        self.close()
        try:
            self._computer = CDPComputer(app, port=self.port, url=url or None,
                                         isolated=isolated, background=True)
        except Exception as e:   # e.g. debug port never bound, or a page-less stale instance
            raise HunchError(f"couldn't open {app} over CDP: {e} — "
                             "web.restart() recovers a stale instance") from e
        s = self._computer.session
        if url:
            s.navigate(url)
        s.wait_ready()
        if not isolated and s.signed_out():
            return (f"opened {app} over CDP, but the Hunch profile isn't signed in "
                    f"(now at {s.url()[:70]}). Call web.login(url=...) once to sign in "
                    "in a clearly-marked window; afterwards the profile stays logged in.")
        snap = self._computer.snapshot()
        return f"opened {app} over CDP ({snap.count('[e')} elements visible), focus-free"

    def login(self, url="", app="Google Chrome"):
        """Open a background, banner-tagged window for the HUMAN to sign in once (Hunch
        never sees the password). Fires a desktop notification; the login persists in
        the Hunch profile."""
        from .cdp import CDPComputer, quit_cdp
        self.close()
        quit_cdp(self.port)  # fresh window, no stale-instance reuse
        try:
            self._computer = CDPComputer(app, port=self.port, url=url or None,
                                         isolated=False, background=True)
        except Exception as e:
            raise HunchError(f"couldn't open {app} for login: {e}") from e
        s = self._computer.session
        if url:
            s.navigate(url)
        s.wait_ready()
        s.mark()
        _notify("Switch to the green 'HUNCH — LOG IN HERE' window to sign in.",
                "Hunch needs you to sign in", sound="Ping")
        return ("opened a background, banner-tagged window and sent a notification — "
                "have the user sign in there and leave it open, then continue")

    def restart(self, url="", app="Google Chrome"):
        """Quit and reopen the CDP instance fresh (same persistent profile, login kept).
        Last resort for a truly broken page — kills whatever holds the CDP port."""
        from .cdp import CDPComputer, quit_cdp
        self.close()
        quit_cdp(self.port)
        try:
            self._computer = CDPComputer(app, port=self.port, url=url or None,
                                         isolated=False, background=True)
        except Exception as e:
            raise HunchError(f"couldn't reopen {app} over CDP: {e}") from e
        s = self._computer.session
        if url:
            s.navigate(url)
        s.wait_ready()
        snap = self._computer.snapshot()
        return f"restarted {app} over CDP ({snap.count('[e')} elements)"

    def snapshot(self):
        """The current page as a ref-annotated accessibility tree (focus-free)."""
        self._session()
        return self._computer.snapshot()

    def act(self, actions):
        """Page actions by ref: click / type (replaces field content; picks <select>
        options by visible text) / key / navigate. Returns the updated tree."""
        self._session()
        return self._computer.act(actions)

    def screenshot(self):
        """The CDP page itself as PNG bytes (focus-free — works in the background)."""
        data = self._session().capture_screenshot()
        if not data:
            raise HunchError("could not capture the page")
        return base64.b64decode(data)

    def tabs(self):
        """Open tabs as '[index]* title — url' lines (* = current)."""
        tabs = self._session().tabs()
        if not tabs:
            return "no open tabs"
        return "\n".join(f"[{t['index']}]{'*' if t['current'] else ' '} {t['title']} — {t['url']}"
                         for t in tabs)

    def switch_tab(self, index):
        return self._session().switch_tab(index)

    def fill_login(self, service):
        """Fill the current page's login form from the user's saved Keychain credential.
        The values never enter your program: read here, typed into the page, deleted.
        Domain-bound credentials are refused on other sites (returns a REFUSED string)."""
        s = self._session()
        from .creds import get_credential, has, kind_of
        if not has(service):
            return (f"No saved credential for '{service}'. See list_credentials(), or add "
                    f"one with: hunch creds add {service}")
        if kind_of(service) == "secret":
            return (f"'{service}' is a protected value (API key/token), not a login — use "
                    f"web.fill_secret('{service}', ref) instead.")
        blocked = gate.domain_mismatch(service, self._page_url())
        if blocked:
            return blocked
        username, password = get_credential(service)   # stays in this process; never returned
        if not (username or password):
            return f"Couldn't read the '{service}' credential from the Keychain."
        r = s.fill_login(username, password)
        del username, password
        return (f"filled '{service}': username={r['username_filled']}, "
                f"password={r['password_filled']} (values not returned)")

    def fill_secret(self, service, ref=""):
        """Type the saved protected value (API key/token) for `service` into a field by ref
        (or the focused element). The value never enters your program."""
        s = self._session()
        from .creds import has, kind_of, get_secret
        if not has(service):
            return (f"No saved credential for '{service}'. See list_credentials(), or add "
                    f"one with: hunch creds add {service} --secret")
        if kind_of(service) != "secret":
            return (f"'{service}' is a username+password login — use "
                    f"web.fill_login('{service}') instead.")
        blocked = gate.domain_mismatch(service, self._page_url())
        if blocked:
            return blocked
        secret = get_secret(service)   # stays in this process; never returned
        if not secret:
            return f"Couldn't read the '{service}' secret from the Keychain."
        s.type_text(ref or None, secret)
        del secret
        return (f"filled the '{service}' secret into "
                f"{('field ' + ref) if ref else 'the focused element'} (value not returned)")

    def _page_url(self):
        try:
            return self._computer.session.url()
        except Exception:
            return ""

    def close(self):
        """Detach from the CDP session (best-effort; the browser window stays open)."""
        if self._computer is not None:
            try:
                self._computer.session.close()
            except Exception:
                pass
            self._computer = None


class Files:
    """Focus-free filesystem ops. ~/.hunch (Hunch's own config/credential metadata) is
    protected — operations there return a REFUSED string."""

    def __init__(self, hunch):
        self._h = hunch

    @staticmethod
    def _refused(p, verb):
        return (f"REFUSED: {p} is inside ~/.hunch — Hunch's own config/credential metadata. "
                f"Not {verb} via Hunch; the user can manage it with the `hunch` CLI.")

    def trash(self, paths):
        """Move file(s)/folder(s) to the Trash by path — reversible."""
        if isinstance(paths, str):
            paths = [paths]
        hit = next((p for p in paths if gate.protected(p)), None)
        if hit is not None:
            return self._refused(hit, "deletable")
        return os_ops.trash(paths)

    def move(self, src, dst):
        for p in (src, dst):
            if p and gate.protected(p):
                return self._refused(p, "writable")
        return os_ops.move(src, dst)

    def copy(self, src, dst):
        for p in (src, dst):
            if p and gate.protected(p):
                return self._refused(p, "writable")
        return os_ops.copy(src, dst)

    def mkdir(self, path):
        if gate.protected(path):
            return self._refused(path, "writable")
        return os_ops.make_dir(path)

    def open(self, path, app=None):
        """Open a file/folder/URL/deep-link with its default (or a named) app."""
        return os_ops.open_path(path, app)

    def reveal(self, paths):
        """Reveal item(s) in Finder (this one does bring Finder forward)."""
        return os_ops.reveal(paths)


class Clipboard:
    def __init__(self, hunch):
        self._h = hunch

    def get(self):
        return os_ops.clipboard_read()

    def set(self, text):
        return os_ops.clipboard_write(text)
