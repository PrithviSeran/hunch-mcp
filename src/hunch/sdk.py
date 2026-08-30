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
from . import policy as _policy_mod
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
                 simultaneous=False, cdp_port=None,
                 snapshot_max_depth=None, snapshot_max_nodes=None,
                 provider="claude", auth=None, can_use_tool=None, app_name=None, policy=None,
                 app_id=None, cdp_profile=None, notify=None):
        """provider: which LLM vendor drives the agent loop — 'claude' (Anthropic, on your
        Claude sign-in; the default) or 'codex' (OpenAI Codex, on a `codex login` session).
        Set once here; then mac.login() / mac.status() / mac.logout() and mac.agent all act
        on it — no provider prefix. Both drive Hunch on a subscription/login (API keys are
        intentionally not exposed).

        auth: an explicit injected credential for the provider, or None to use its own stored
        sign-in. For 'claude' this is hunch.OAuthToken(...) (the subscription token the app
        mints); 'codex' takes no injected credential (authenticate with mac.login() /
        `codex login`).

        can_use_tool: optional host-owned permission callback (tool_name, input, context) ->
        Allow/Deny, handed to the provider's backend so a host app can route every tool
        through its own Approve/Deny UI.

        confirm: 'dialog' (osascript click-to-approve), 'off' (auto-approve everything),
        or a callable(ConsentRequest) -> bool to render consent in YOUR app's UI.

        policy: which gates are active — None (ALL gates on, instance-owned: the uniform
        default; the machine's ~/.hunch/config.json is never consulted), a dict
        ({"gates": {"shell": False, ...}, "auto_approve_all": bool}), a callable
        (category) -> bool, or 'personal' (the live config-file resolver — what
        `hunch serve` uses so `hunch config set` keeps applying to the MCP server).

        app_name: brands consent dialogs and notifications (default: derived from app_id,
        else 'Hunch').

        app_id: PURE NAMESPACING, never a behavior switch — a stable reverse-DNS id for
        the app you're shipping ("com.acme.mailbot"). It only picks storage names, so two
        apps built on the SDK coexist on one Mac: their own Keychain token slot and
        credential store, their own browser profile and derived CDP port. None -> the
        legacy/personal names (what the MCP server uses).

        cdp_profile: browser profile dir override (default derives from app_id).
        notify: callable(message, title) replacing system notifications with your own
        surface (e.g. an in-app toast)."""
        if isinstance(policy, dict):
            unknown = set(policy.get("gates", {})) - set(_policy_mod.DEFAULT_GATES)
            if unknown:
                raise HunchError(f"unknown gate(s) in policy: {sorted(unknown)} — valid: "
                                 f"{sorted(_policy_mod.DEFAULT_GATES)}")
        if app_id is not None:
            if not app_id or not all(c.isalnum() or c in "._-" for c in app_id):
                raise HunchError(f"invalid app_id {app_id!r} — use letters, digits, '.', "
                                 "'_', '-' (reverse-DNS style, e.g. 'com.acme.mailbot')")
        if notify is not None and not callable(notify):
            raise HunchError("notify must be a callable(message, title) or None")
        self.app_id = app_id
        self._app_id = app_id            # what Agent/_SubscriptionRunner read
        self._notify_handler = notify
        self.app_name = app_name or (app_id.split(".")[-1].replace("-", " ").replace("_", " ")
                                     .title() if app_id else "Hunch")
        try:
            self._gate = gate.Gate(confirm=confirm, app_name=self.app_name, policy=policy)
        except ValueError as e:
            raise HunchError(str(e)) from None
        from . import providers as _provider_mod
        if provider not in _provider_mod.PROVIDERS:
            raise HunchError(f"unknown provider {provider!r} — use "
                             + ", ".join(repr(n) for n in _provider_mod.PROVIDERS))
        from .auth import OAuthToken
        if not (auth is None or isinstance(auth, OAuthToken)):
            raise HunchError(f"unknown auth {auth!r} — pass hunch.OAuthToken(...) (the Claude "
                             "subscription token) or None; authenticate codex with mac.login()")
        if isinstance(auth, OAuthToken) and provider != "claude":
            raise HunchError("auth=OAuthToken(...) is a Claude credential, but "
                             f"provider={provider!r} was requested")
        self._provider_name = provider
        self._provider = _provider_mod.provider(provider)
        self._agent_auth = auth
        self._can_use_tool = can_use_tool
        if check_permissions:
            self._check_accessibility()
        # ONE persistent computer per instance, so element [refs] survive snapshot -> act.
        self._computer = LocalComputer(app=app, simultaneous=simultaneous,
                                       max_depth=snapshot_max_depth,
                                       max_nodes=snapshot_max_nodes)
        # Last app aim'd by focus_app / launch_app / snapshot(app=...). Empty
        # snapshot() prefers this over frontmost so a bare snapshot after
        # focus_app("System Settings") does not silently read Cursor.
        self._aimed_app = None
        # Namespaced defaults (names only — same semantics): derived CDP port + profile.
        if app_id and cdp_port is None:
            import zlib
            cdp_port = 9400 + (zlib.crc32(app_id.encode()) % 500)
        if app_id and cdp_profile is None:
            import os as _os
            cdp_profile = _os.path.expanduser(f"~/.hunch/apps/{app_id}/chrome-cdp")
        self.web = Web(self, cdp_port or CDP_PORT, profile=cdp_profile)
        self.files = Files(self)
        self.clipboard = Clipboard(self)
        self._agent = None   # the agent loop, created lazily so the LLM SDKs stay optional

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

    def snapshot(self, app="", ref=None, max_depth=None, max_nodes=None, max_children=None):
        """The app's focused window as a ref-annotated accessibility tree (focus-free).
        Empty `app` targets the last aimed app (focus_app / launch_app /
        snapshot(app=...)), else the frontmost app. Pass ref="e42" to expand ONLY
        that element's subtree at full depth (other refs stay valid). Truncation is
        never silent: capped output ends in an explicit …marker naming the ref to
        expand. max_children raises the per-node sibling cap to page a big list in."""
        if ref is not None:
            return self._computer.snapshot(ref=ref, max_depth=max_depth, max_nodes=max_nodes,
                                           max_children=max_children)
        prev, prev_aimed = self._computer.app, self._aimed_app
        if app:
            self._computer.app = app
            self._aimed_app = app
        else:
            self._computer.app = self._aimed_app or _frontmost()
        out = self._computer.snapshot(max_depth=max_depth, max_nodes=max_nodes,
                                      max_children=max_children)
        if isinstance(out, str) and out.startswith("(app '") and "not found" in out:
            # a failed target must not poison later calls
            self._computer.app, self._aimed_app = prev, prev_aimed
        return out

    def find(self, role=None, name_contains=None, app="", max_results=20):
        """Search an app's WHOLE accessibility tree (deeper than snapshot shows) and
        return only matching elements, each with an actable [ref]. role is
        case-insensitive with the AX prefix optional ("button" == AXButton);
        name_contains matches title/description/value as a substring."""
        prev, prev_aimed = self._computer.app, self._aimed_app
        if app:
            self._computer.app = app
            self._aimed_app = app
        elif self._aimed_app:
            self._computer.app = self._aimed_app
        out = self._computer.find(role=role, name_contains=name_contains, max_results=max_results)
        if isinstance(out, str) and out.startswith("(app '") and "not found" in out:
            self._computer.app, self._aimed_app = prev, prev_aimed
        return out

    def act(self, actions, reason="", confirm=False):
        """Run UI actions (same dicts as the MCP `act` tool: click/select/right_click/type/
        menu/key/click_xy by ref) and return the updated tree. Focus-stealing actions are
        gated; a user refusal raises ApprovalDenied. StaleRef means re-snapshot.
        confirm=True skips the dialog (the user already approved out-of-band)."""
        # Coerce before the focus-steal gate so a stringified actions payload
        # (local-model failure mode) does not blow up on a.get inside the gate.
        coerced, bad = LocalComputer._coerce_actions(actions)
        if bad:
            return bad
        actions = coerced
        blocked = gate.check_focus_steal(self._computer, actions, self._gate,
                                         confirm=confirm, reason=reason)
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
        self._aimed_app = name
        refs = self._computer.snapshot().count("[e")
        return (f"{msg}; accessibility tree has {refs} elements"
                + ("" if refs > 15 else
                   " (still low — if it's an Electron app, retry with force_accessibility=True)"))

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
        self._aimed_app = name
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

    def applescript(self, script, confirm=False):
        """Run AppleScript against scriptable apps (Mail, Music, Finder, …) focus-free.
        Mutating/'do shell script' scripts are gated; refusal raises ApprovalDenied.
        confirm=True skips the dialog (the user already approved out-of-band).
        System Settings toggles via defaults write / UI scripting are soft-refused with a
        teaching string — AX snapshot/act is the reliable path."""
        refusal = gate.applescript_settings_refusal(script)
        if refusal:
            return refusal
        category = gate.applescript_category(script)
        if category and not confirm and self._gate.enabled(category):
            preview = script.strip()[:400].replace("\n", "  ").replace("\r", " ")
            if not self._gate.confirm_dialog(
                    f"{self.app_name} wants to run an AppleScript that can change things or "
                    f"control apps:  {preview}   — allow?", screen_approval=False,
                    category=category, detail=preview):
                raise ApprovalDenied("user did not approve the AppleScript")
        ok, out = os_ops.run_applescript(script)
        if ok:
            hint = "" if out else gate.applescript_empty_hint(script)
            return f"(no output){hint}" if hint else out
        return f"AppleScript error: {out[:600]}{gate.applescript_hint(out)}"

    def notify(self, message, title=None):
        """Notify the user: through the instance's notify handler if one was given
        (your app's own surface), else a macOS desktop notification titled app_name."""
        title = title or self.app_name
        if self._notify_handler is not None:
            self._notify_handler(message, title)
            return
        _notify(message, title, sound="Ping")

    def list_credentials(self):
        """Names + kinds of saved credentials (never any values). Fill them into a CDP
        page with web.fill_login(service) / web.fill_secret(service, ref)."""
        from .creds import list_services, kind_of
        names = list_services(self.app_id)
        if not names:
            return "No saved credentials. Add them with: hunch creds add <service>"
        logins = [n for n in names if kind_of(n, self.app_id) != "secret"]
        secrets = [n for n in names if kind_of(n, self.app_id) == "secret"]
        parts = []
        if logins:
            parts.append("logins (web.fill_login): " + ", ".join(logins))
        if secrets:
            parts.append("API keys/secrets (web.fill_secret): " + ", ".join(secrets))
        return "Saved credentials — " + "; ".join(parts)

    # ── provider (auth + agent loop) ──────────────────────────────────────────

    @property
    def provider(self):
        """The configured LLM provider ('claude' | 'codex'), as a Provider object.
        Usually you go through mac.login()/status()/logout()/agent instead."""
        return self._provider

    def login(self, **kwargs):
        """Sign in to the configured provider (interactive) — claude: your Claude sign-in;
        codex: a ChatGPT/Codex browser sign-in. Returns an AuthStatus."""
        return self._provider.login(app_id=self._app_id, **kwargs) \
            if self._provider_name == "claude" else self._provider.login(**kwargs)

    def logout(self, **kwargs):
        """Sign the configured provider out."""
        return self._provider.logout(app_id=self._app_id, **kwargs) \
            if self._provider_name == "claude" else self._provider.logout(**kwargs)

    def status(self, **kwargs):
        """Who, if anyone, is signed in for the configured provider (an AuthStatus)."""
        return self._provider.status(app_id=self._app_id, **kwargs) \
            if self._provider_name == "claude" else self._provider.status(**kwargs)

    @property
    def agent(self):
        """The agent loop: `mac.agent.run(task=...)` runs an LLM loop that drives this Mac
        through these primitives, on the provider chosen at construction (Hunch(provider=...)).
        Created lazily so plain use imports none of the LLM SDKs."""
        if self._agent is None:
            from .agent import Agent
            self._agent = Agent(self, provider=self._provider, auth=self._agent_auth,
                                can_use_tool=self._can_use_tool)
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
    """Focus-free browser/Electron control over CDP, on a persistent profile. With no
    app_id the port/profile are the shared personal ones (whichever process opened the
    browser first is gracefully reused); with an app_id each app gets its OWN port and
    profile, so restart()/login() recovery can only ever kill that app's browser."""

    def __init__(self, hunch, port, profile=None):
        self._h = hunch
        self.port = port
        self.profile = profile      # None -> cdp.HUNCH_PROFILE (the personal default)
        self.force_sandbox = None   # True forces isolated profiles; None defers to
                                    #   the HUNCH_FORCE_SANDBOX env var (the app toggle)
        self._computer = None       # per-instance CDPComputer (the server's _cdp equivalent)

    def _session(self):
        if self._computer is None:
            raise WebNotOpen("no web/Electron app open — call web_open / web.open() first")
        return self._computer.session

    def open(self, url="", app="Google Chrome", isolated=False):
        """Open a Chromium browser (or Electron app) for focus-free control. Uses the
        persistent, dedicated Hunch profile (isolated=True: throwaway sandbox profile).
        If the profile isn't signed into the site, the returned string says so — call
        login() once, then reopen.

        For a code editor (app="Cursor"/"Visual Studio Code"/…), `url` is the FOLDER or FILE
        to open, and Hunch drives a DEDICATED editor window (its own profile+port, separate
        from your own editor). Its integrated terminal is then typeable focus-free via act()
        — the AX tree can't write xterm.js, but CDP injects real keystrokes into the PTY."""
        from .cdp import _is_editor
        if _is_editor(app):
            return self._open_editor(folder=url, app=app)
        import os as _os
        force = (self.force_sandbox if self.force_sandbox is not None
                 else _os.environ.get("HUNCH_FORCE_SANDBOX") == "1")
        if force:
            isolated = True  # throwaway, no-login profile (the app's Sandbox toggle)
        from .cdp import CDPComputer
        self.close()
        try:
            self._computer = CDPComputer(app, port=self.port, url=url or None,
                                         isolated=isolated, background=True,
                                         profile=self.profile)
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

    def _open_editor(self, folder="", app="Cursor"):
        """Open a code editor (Cursor/VS Code/…) on `folder` in a DEDICATED, background Hunch
        window and drive it over CDP. Non-destructive: it's a separate instance on its own
        profile+port, so the user's own editor is untouched. The integrated terminal becomes
        focus-free-typeable (act 'type' on the terminal element, or key ctrl+` to open one)."""
        import os as _os
        from .cdp import CDPComputer, editor_target, open_folder_in_editor
        port, profile, real = editor_target(app)
        # Check the path BEFORE launching anything: a typo'd folder otherwise costs a launch plus
        # ~40s of polling for a window that can never appear, and ends in a vague failure.
        if folder and not _os.path.exists(_os.path.expanduser(folder)):
            return (f"no such path: {folder!r} — nothing was opened. Check it exists (and is the "
                    f"folder you meant) before opening it in {real}.")
        folder = _os.path.abspath(_os.path.expanduser(folder)) if folder else folder
        _os.makedirs(profile, exist_ok=True)
        self.close()
        try:
            self._computer = CDPComputer(real, port=port, url=folder or None,
                                         isolated=False, background=True, profile=profile,
                                         editor=True)
        except Exception as e:
            raise HunchError(f"couldn't open {real} over CDP: {e} — "
                             "web.restart() recovers a stale instance") from e
        s = self._computer.session
        s.wait_ready()
        if folder:
            # VERIFY, never assume. A live editor instance is REUSED as-is, so the folder passed at
            # launch is silently dropped and the session lands on whatever window that instance
            # already had (commonly a workspace restored from a previous session). Reporting the
            # requested folder here — without checking — is what sent the agent driving a stranger's
            # project for a dozen turns while every message said it was on the right one.
            # Generous first wait: a cold editor renders its workbench (and so gets its title)
            # seconds after the debug port binds, and opening a duplicate window is the cost of
            # giving up early.
            if not s.bind_workspace(folder, timeout=12):
                open_folder_in_editor(real, profile, folder)   # hand the folder to the live instance
                s.bind_workspace(folder, timeout=30)
            if not s.pinned:
                return self._editor_mismatch(real, folder, s)
        snap = self._computer.snapshot()
        where = f" on {folder}" if folder else ""
        has_term = ".xterm" in snap or "xterm-helper" in snap
        tip = ("Its terminal is open — act a 'type' on the terminal element to run commands "
               "focus-free." if has_term else
               "No terminal open yet — act key ctrl+` to open one, web_snapshot, then 'type' into it.")
        return (f"opened {real}{where} over CDP ({snap.count('[e')} elements), focus-free — a "
                f"dedicated Hunch editor window, separate from your own. {tip}")

    @staticmethod
    def _editor_mismatch(real, folder, s):
        """The requested folder never came up. Say so plainly, name what IS open, and hand over the
        one recovery that works — driving the bound window's own Open Recent. Never dress a
        wrong-workspace session up as a success: the agent cannot tell from the tree alone."""
        wins = s.windows()
        have = ", ".join("[{}] {!r}".format(w["index"], w["workspace"] or w["title"])
                         for w in wins) or "none"
        return (f"could NOT open {folder!r} in {real} — this session is on workspace "
                f"{s.workspace()!r}, NOT the folder you asked for. Do not act on this window "
                f"expecting {folder!r}. Its open windows: {have}. "
                f"Recovery, in order: web_switch_tab(i) if one of those windows IS the folder; "
                f"otherwise switch THIS window's workspace from inside it — web_act key "
                f"cmd+shift+p, type '>File: Open Recent', click the folder's row (that keeps the "
                f"window CDP is bound to, which opening a new window does not).")

    def login(self, url="", app="Google Chrome"):
        """Open a background, banner-tagged window for the HUMAN to sign in once (Hunch
        never sees the password). Fires a desktop notification; the login persists in
        the Hunch profile."""
        from .cdp import CDPComputer, quit_cdp
        self.close()
        quit_cdp(self.port)  # fresh window, no stale-instance reuse
        try:
            self._computer = CDPComputer(app, port=self.port, url=url or None,
                                         isolated=False, background=True,
                                         profile=self.profile)
        except Exception as e:
            raise HunchError(f"couldn't open {app} for login: {e}") from e
        s = self._computer.session
        if url:
            s.navigate(url)
        s.wait_ready()
        s.mark()
        self._h.notify("Switch to the green 'HUNCH — LOG IN HERE' window to sign in.",
                       f"{self._h.app_name} needs you to sign in")
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
                                         isolated=False, background=True,
                                         profile=self.profile)
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
        """Page actions: click by ref; click_xy/drag at web-screenshot coordinates; type
        (replaces a referenced field, or types at focus without a ref); key; navigate."""
        self._session()
        return self._computer.act(actions)

    def screenshot(self):
        """The CDP page itself as PNG bytes (focus-free — works in the background)."""
        data = self._session().capture_screenshot()
        if not data:
            raise HunchError("could not capture the page")
        return base64.b64decode(data)

    def tabs(self):
        """Open tabs as '[index]* title — url' lines (* = current). For an EDITOR these are
        WINDOWS, listed by WORKSPACE: they all share one workbench.html url, so the url column
        (what this used to print) could not tell two projects apart."""
        s = self._session()
        if getattr(s, "editor", False):
            wins = s.windows()
            if not wins:
                return "no editor windows"
            return ("editor WINDOWS — each is its own workspace; web_switch_tab(i) binds one:\n"
                    + "\n".join(f"[{w['index']}]{'*' if w['current'] else ' '} {w['title']}"
                                + (f"   [workspace: {w['workspace']}]" if w['workspace'] else "")
                                for w in wins))
        tabs = s.tabs()
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
        ns = self._h.app_id
        from .creds import get_credential, has, kind_of
        if not has(service, ns):
            return (f"No saved credential for '{service}'. See list_credentials(), or add "
                    f"one with: hunch creds add {service}")
        if kind_of(service, ns) == "secret":
            return (f"'{service}' is a protected value (API key/token), not a login — use "
                    f"web.fill_secret('{service}', ref) instead.")
        blocked = gate.domain_mismatch(service, self._page_url(),
                                       app_name=self._h.app_name, namespace=ns)
        if blocked:
            return blocked
        username, password = get_credential(service, ns)   # stays in this process; never returned
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
        ns = self._h.app_id
        from .creds import has, kind_of, get_secret
        if not has(service, ns):
            return (f"No saved credential for '{service}'. See list_credentials(), or add "
                    f"one with: hunch creds add {service} --secret")
        if kind_of(service, ns) != "secret":
            return (f"'{service}' is a username+password login — use "
                    f"web.fill_login('{service}') instead.")
        blocked = gate.domain_mismatch(service, self._page_url(),
                                       app_name=self._h.app_name, namespace=ns)
        if blocked:
            return blocked
        secret = get_secret(service, ns)   # stays in this process; never returned
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
        if self._h._gate.enabled("destructive_file"):
            preview = ", ".join(str(p)[:60] for p in paths[:3])
            if len(paths) > 3:
                preview += f" … (+{len(paths) - 3} more)"
            if not self._h._gate.confirm_dialog(
                f"{self._h.app_name} wants to move {len(paths)} item(s) to Trash: {preview} — allow?",
                category="destructive_file", detail=preview, screen_approval=False):
                from .gate import ApprovalDenied
                raise ApprovalDenied("user did not approve moving items to Trash")
        return os_ops.trash(paths)

    def move(self, src, dst):
        for p in (src, dst):
            if p and gate.protected(p):
                return self._refused(p, "writable")
        if self._h._gate.enabled("destructive_file"):
            detail = f"{src} -> {dst}"
            if not self._h._gate.confirm_dialog(
                f"{self._h.app_name} wants to move {src!r} to {dst!r} — allow?",
                category="destructive_file", detail=detail, screen_approval=False):
                from .gate import ApprovalDenied
                raise ApprovalDenied("user did not approve move")
        return os_ops.move(src, dst)

    def copy(self, src, dst):
        for p in (src, dst):
            if p and gate.protected(p):
                return self._refused(p, "writable")
        if self._h._gate.enabled("destructive_file"):
            detail = f"{src} -> {dst}"
            if not self._h._gate.confirm_dialog(
                f"{self._h.app_name} wants to copy {src!r} to {dst!r} — allow?",
                category="destructive_file", detail=detail, screen_approval=False):
                from .gate import ApprovalDenied
                raise ApprovalDenied("user did not approve copy")
        return os_ops.copy(src, dst)

    def mkdir(self, path):
        if gate.protected(path):
            return self._refused(path, "writable")
        if self._h._gate.enabled("destructive_file"):
            if not self._h._gate.confirm_dialog(
                f"{self._h.app_name} wants to create folder {path!r} — allow?",
                category="destructive_file", detail=str(path), screen_approval=False):
                from .gate import ApprovalDenied
                raise ApprovalDenied("user did not approve mkdir")
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
