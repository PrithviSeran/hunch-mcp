"""hunch_mcp.py — drive your REAL Mac (Hunch local backend) over MCP.

Point Claude Desktop at this and you control your machine through your Claude
subscription instead of a metered API key — no per-token spend. It exposes the
same tree-native primitives as the direct module, backed by local_mac.py.

Requires Accessibility permission for whatever process runs this (grant
Claude Desktop in System Settings → Privacy & Security → Accessibility).
"""
import os
import base64

from mcp.server.fastmcp import FastMCP, Image
from AppKit import NSWorkspace

from .local_mac import (LocalComputer, screenshot_b64, list_running_apps, set_focus_reason,
                       launch_app as _launch_app, quit_app as _quit_app, focus_app as _focus_app)
from . import gate
from . import os_ops
from .notify import notify as _notify_impl

HUNCH_PLAYBOOK = """Hunch drives this Mac across THREE focus-free layers plus a gated last resort.
ALWAYS pick the most direct layer for the task — it's faster, more reliable, and (except the
last) never disturbs the user's screen. Prefer a direct API over clicking a UI.

NATIVE-FIRST: when the service behind a task has a native Mac app INSTALLED, do the task in that
app — NOT its website. Controlling real apps is Hunch's edge over a browser bot: email → Mail.app
(not gmail.com), calendar → Calendar.app, texts → Messages, notes/todos → Notes/Reminders, music →
the Music/Spotify app (not open.spotify.com), files → Finder/OS-API (not drive.google.com for local
files). Check `list_apps` (or launch_app's error) if unsure what's installed. Drop to the website
only when: no native app is installed, the native app isn't signed into the needed account (e.g.
Mail has no accounts configured), or the capability genuinely exists only on the web — and say
which of those it was. The browser is the fallback, not the default.
STAY NATIVE even for embedded-Chromium apps (Electron/CEF: Discord, Slack, Spotify…). Even hardened
ones that strip the CDP debug port ARE readable focus-free as a TREE: `snapshot(app)` auto-relaunches
the app once with the accessibility flag (focus-free `open -g`; the app restores its prior view) so
its full web UI appears in the background AX tree — verified on Discord (200+ refs: servers, DMs,
unread counts, usernames — read while another app was frontmost). So `snapshot`/`act` are the path,
same as any native app. Only if the tree STILL reads empty after that (a truly locked-down app) do
you fall to VISION (`screenshot` + gated `click_xy`/`key`). Do NOT reroute to the service's website —
that's the browser-bot path Hunch exists to beat; go to the web only if the user asks.

LAYERS (most-direct first):
1) OS-API — files, clipboard, apps. `trash(paths)` to DELETE files (never Finder + ⌘⌫);
   `file_op(move/copy/mkdir)`; `open_file` for files/URLs/app deep-links; `clipboard_get/set`
   instead of ⌘C/⌘V; `launch_app`/`quit_app`/`focus_app`/`list_apps` for app lifecycle.
2) AppleScript — `applescript(script)` gives focus-free control of scriptable native apps:
   Mail (send/read), Messages, Notes, Reminders, Calendar, Music/Spotify (playback), Finder,
   Safari. Prefer this over clicking those apps' UI. Risky scripts auto-prompt the user.
3) Web + Electron via CDP — `web_open`/`web_snapshot`/`web_act`/`web_login`/`web_restart`/
   `web_screenshot` drive any browser or Electron app focus-free. READ a page with `web_snapshot`,
   NOT the OS `screenshot` (that grabs the physical screen = the user's own window in the background);
   `web_screenshot` is the focus-free way to see the CDP page as pixels. FORMS: `web_act` "type"
   REPLACES a field and picks a native <select> option by its visible text (type "January") — NEVER
   click a native dropdown.
   NEW TABS: if a click/form opens a new tab, the next `web_snapshot` AUTO-FOLLOWS it — so just
   snapshot again to reach the new page. `web_tabs` lists open tabs; `web_switch_tab(i)` moves
   deliberately (back to a prior tab, or if auto-follow picked the wrong one).
   LINKS: to follow a link ("Apply", "Sign in", etc.) CLICK it by ref — its href/redirect knows the
   real destination. NEVER `navigate` to a URL you guessed or constructed (e.g. apply.<site>.com);
   only navigate to URLs the user gave you or that you read from the page. A guessed URL usually
   404s / doesn't resolve and sends you nowhere.
4) Native UI via Accessibility — `snapshot`/`act` for NATIVE (AppKit) app UIs. Focus-free primitives:
   click / select / right_click / type-into-a-field / `menu` (run a menu-bar command by path —
   use it INSTEAD of a keyboard shortcut). `key` and `click_xy` STEAL FOCUS and auto-prompt the
   user — last resort only, when no menu/field/API equivalent exists.
   EMBEDDED-CHROMIUM apps — Electron (Cursor, VS Code, Discord, Slack, …) or CEF (e.g. Spotify): their
   UI is web content that Chromium only keeps in the AX tree while active. `snapshot(app)` handles this
   FOR you — it relaunches the app once, focus-free, with --force-renderer-accessibility so the full
   tree stays live in the background (a brief one-time blink; the app restores its view). After that,
   read/act on it like any native app, no focus cost. Real Electron ALSO exposes a CDP debug port, so
   web_open (layer 3) is an alternative; but hardened apps (Discord) strip that port, and the AX-tree
   path above works on them anyway. If the app is the user's actively-open editor, prefer NOT to drive
   it. Only if the tree stays empty even after the force relaunch is the app truly locked — see below.

KEY WORKFLOWS:
- Sign into a site: `web_login(app, url)` opens a background banner-tagged window and notifies
  the user; then wait for their "done".
- Log in with SAVED credentials: if `list_credentials` shows a service, `web_open` the site then
  `web_fill_login(service)` — Hunch types the saved email+password straight into the page and you
  NEVER see the values; then submit via `web_act`. No saved credential? Use `web_login` instead.
- Enter a SAVED API key/secret: `web_fill_secret(service, ref)` types the protected value straight
  into the given field (ref from `web_snapshot`) — you never see it. Never ask the user to paste a
  key into chat if a saved secret exists.
- Page stuck/blank/slow: first WAIT and re-`web_snapshot` (give a heavy page 15-30s). Restart
  only if it's truly broken (error page / unchanged) via `web_restart` — do NOT grind on it.
- Page tree shows only NAV/SIDEBAR, main content missing: it's lazy-loaded / below the fold / still
  hydrating — SCROLL and re-read (`web_act` [{"action":"key","key":"PageDown"}], focus-free via CDP)
  or wait and `web_snapshot` again. This is NORMAL, not a broken page. NEVER reach for the OS
  `screenshot` tool to see a web page — it captures the physical frontmost screen, so on a background
  CDP window you get the USER's own window, not the page. If you truly need the page as pixels
  (chart/canvas/image), use `web_screenshot` (focus-free, captures the CDP page itself).
- You need the human (sign-in, 2FA, a decision, review-before-submit): call `notify_user(msg)`
  so they get a desktop alert — never stall silently.
- APP RESISTS EVERY LAYER: `snapshot` already auto-relaunches an embedded-Chromium app with the
  accessibility flag, so most Electron/CEF apps DO yield a tree. Only if `snapshot` STILL reads an
  empty/near-empty tree after that one-time force relaunch (and `web_open` can't attach a debug port)
  is the app truly locked. Don't grind (no repeated launches/restarts — a restart can also disrupt the
  user, e.g. interrupt playback). Fall back in order, STAYING NATIVE: (1) its AppleScript dictionary
  if it has one (media/player apps are commonly PLAYBACK-ONLY — no library/downloads/content control);
  (2) VISION on the native app — `screenshot` to read the screen, then the gated `click_xy`/`key`
  actions to drive it (they ask the user before taking the screen; needs Screen Recording). Announce
  the focus steal, work quickly, restore the user's frontmost app after. (3) The service's website is
  the LAST resort — only if the user prefers it. Be honest about which path you're on.
- DOING A TASK ON A WEBSITE (make an account, apply, sign up, book): navigate like a HUMAN, don't
  guess URLs. 1) `web_open` the site's HOMEPAGE / root domain (e.g. https://a16z.com) — NOT a guessed
  deep path like /apply or /signup. 2) `web_snapshot` to READ it. 3) Find the real link/button for the
  goal (Sign up, Log in, Apply, Get started, Menu) in the tree and CLICK it by ref. 4) `web_snapshot`
  again (new tabs auto-follow) and repeat, working page by page to the actual form. If you can't find
  the link, scroll (`key` PageDown) or open the nav/menu — NEVER fall back to typing a guessed URL.
- Filling a form: `web_snapshot`, then `web_act` "type" each field (replaces + handles dropdowns).
  Don't submit unless told; read fields back so the user can review.

DESTRUCTIVE / SHELL / PRIVILEGED OPS:
- Prefer `trash` (reversible) over `rm`; only empty the Trash when the task truly needs the space
  back — emptying removes trash's undo.
- Running shell (`do shell script`): check the exit code and RE-VERIFY the effect (e.g. re-measure
  freed space). NEVER mask a destructive command with `; echo ok` — echo succeeds even when `rm`
  fails, so a no-op looks like success.
- Protected paths (CoreSimulator, /Library/Developer, SIP): admin `rm` hits TCC "Operation not
  permitted" — don't retry variants; notify the user and have them `sudo` from a Full-Disk-Access
  Terminal.

RULES:
- You have 20+ tools across these layers — scan the full set before deciding something's impossible.
- Focus-free layers (OS-API / AppleScript / CDP / AX-click) never touch the user's screen; use them
  freely. The focus-stealing actions auto-prompt for approval, so don't fear them — but prefer an
  alternative (menu action, applescript, or a direct API) whenever one exists.
- Confirm with the user before any consequential/irreversible action (send, delete, submit, purchase)."""

mcp = FastMCP("hunch", instructions=HUNCH_PLAYBOOK)

# When the host owns permissions (e.g. the Hunch app's own approve UX), HUNCH_NO_INTERNAL_GATE=1
# suppresses the duplicate desktop banners here; its gate suppression flows through
# policy.gate_enabled (env > auto_approve_all > per-category, from ~/.hunch/config.json).
_APP_OWNS_PERMS = bool(os.environ.get("HUNCH_NO_INTERNAL_GATE"))
# ONE persistent computer, so element [refs] survive across snapshot -> act calls.
_computer = LocalComputer(app="Finder")


def _frontmost():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.localizedName() if app else "Finder"


# The consent/safety layer lives in gate.py (shared with the Python SDK); this server keeps
# ONE Gate so approval state behaves exactly as the old module globals did.
_gate = gate.Gate(confirm="dialog")
_as_str = gate.as_str


def _notify(message, title="Hunch"):
    """Best-effort macOS desktop notification (so the user knows Hunch needs them).
    In app mode the Hunch popover re-surfaces itself instead (the app pops it when the
    notify_user / web_login tool event streams through), so the banner would be a duplicate."""
    if _APP_OWNS_PERMS:
        return
    try:
        _notify_impl(message, title, sound="Ping")
    except Exception:
        pass


def _confirm_dialog(prompt, timeout=180, screen_approval=True):
    return _gate.confirm_dialog(prompt, timeout, screen_approval)


def _mark_screen_approval():
    _gate.mark_screen_approval()


def _screen_approved():
    return _gate.screen_approved()


def _front_gate(name, reason=""):
    return _gate.front_gate(name, reason)


@mcp.tool()
def snapshot(app: str = "", ref: str = "", max_depth: int | None = None,
             max_nodes: int | None = None) -> str:
    """See the screen as an accessibility tree. Each element is one line tagged with
    a [ref] like [e12]; you act on elements by ref. Pass an app name to target that
    app's focused window, or leave blank for the frontmost app.

    Truncation is never silent: when the tree is bigger than the walk budget, the
    output ends in `…` marker lines naming what was dropped. Pass ref="e42" to
    re-walk ONLY that element's subtree at full depth (its refs match the full
    snapshot's, and every other ref stays valid) — the way to see inside a
    truncated container. `find` locates elements without reading a full tree.
    max_depth/max_nodes override the defaults when you truly need a bigger walk.

    A PARTIAL tree is NOT a dead end. Some apps (chat/mail/master-detail, and Catalyst apps like
    WhatsApp) only expose the pane you're IN — the first snapshot may show just the sidebar/list.
    That does NOT mean the tree is unusable: `click` the item by ref to OPEN it (a chat, an email,
    a conversation), then snapshot AGAIN — the detail pane (compose field, messages, Send) renders
    into the tree once you navigate there. Drive → re-snapshot → drive. Do NOT jump to `screenshot`
    or steal focus (focus_app / launch_app foreground / click_xy) just because the first read was
    thin. Reserve `screenshot` for confirming truly VISUAL content (an image, a file-preview
    thumbnail) — to check what an action did or read UI text, RE-SNAPSHOT, don't screenshot."""
    if ref:
        return _computer.snapshot(ref=ref, max_depth=max_depth, max_nodes=max_nodes)
    prev = _computer.app
    _computer.app = app or _frontmost()
    out = _computer.snapshot(max_depth=max_depth, max_nodes=max_nodes)
    if isinstance(out, str) and out.startswith("(app '") and "not found" in out:
        _computer.app = prev   # a failed target must not poison later act/snapshot calls
    return out


@mcp.tool()
def find(role: str = "", name_contains: str = "", app: str = "", max_results: int = 20) -> str:
    """Search an app's WHOLE accessibility tree for matching elements — the cheap way
    to locate one control in a huge window without reading a full snapshot. Filters:
    `role` (e.g. "button", "AXTextField", "row" — case-insensitive, "AX" prefix
    optional) and/or `name_contains` (case-insensitive substring of the element's
    title/description/value). Returns one line per match: an ancestor breadcrumb,
    then the element tagged with a [ref] you can pass straight to `act`
    (click/select/type by ref) or to `snapshot(ref=...)` to expand its subtree.
    Searches deeper than `snapshot` shows, so it reaches elements a truncated
    snapshot omitted. Refs stay valid until the next full snapshot of that app.
    Pass an app name or leave blank for the current target app."""
    prev = _computer.app
    if app:
        _computer.app = app
    out = _computer.find(role=role or None, name_contains=name_contains or None,
                         max_results=max_results)
    if isinstance(out, str) and out.startswith("(app '") and "not found" in out:
        _computer.app = prev
    return out


@mcp.tool()
def act(actions: list, confirm: bool = False, reason: str = "") -> str:
    """Execute one or more UI actions in order (by element ref), then return the
    updated tree. Each action is an object:
      {"action":"click","ref":"e12"}                # press/activate it — OPENS a chat/email/file (focus-free)
      {"action":"right_click","ref":"e12"}          # open its context menu (focus-free)
      {"action":"select","ref":"e12"}               # only HIGHLIGHTS a row — does NOT open it (focus-free)
      {"action":"menu","path":["File","Move to Trash"]}  # invoke a menu-bar command (FOCUS-FREE)
      {"action":"type","ref":"e12","text":"hello"}  # into a ref = focus-free; no ref = types at focus (STEALS FOCUS)
      {"action":"key","key":"return","modifiers":["command"]}   # keystroke (STEALS FOCUS)
      {"action":"click_xy","x":640,"y":400}         # pixel fallback, last resort (STEALS FOCUS)
    Prefer the focus-free primitives. For a keyboard shortcut (⌘⌫ move-to-trash, ⌘S save,
    ⌘W close, ⌘N new, ⌘F find …) use a `menu` action with the menu-bar path instead of `key`
    — it runs the SAME command focus-free. Reserve `key` for things with no menu/field
    equivalent (typing into a canvas, arrow-keys in a game). Use click_xy only when an element
    isn't in the tree.
    NEVER reach for these keystrokes — each has a focus-free tree equivalent, and the keystroke
    just gets auto-refused in the background anyway:
      • SUBMIT / SEND / CONFIRM (Return, Enter, ⌘Return) → `click` the button by ref (Send, Open,
        Save, OK, Next). A message field's Send button, a dialog's default button — they're in the
        tree; click them. Return is a keystroke; the button is a ref.
      • PASTE (⌘V) → `menu` ["Edit","Paste"], or better, `type` the text straight into the field ref
        (skip the clipboard entirely). COPY (⌘C) → `menu` ["Edit","Copy"]. SELECT ALL (⌘A) →
        `menu` ["Edit","Select All"].
    ATTACHING A FILE: pasting the file into the message (put it on the clipboard, then `menu`
    ["Edit","Paste"]) is usually FASTER and more reliable than driving the app's Open/file panel —
    those panels' Open button often won't enable from a background AX click (its selection binding
    wants the panel key). If you must use the panel: `select` the file row (not `click`) so the
    selection registers, then `click` the Open button by ref — never fall back to Return/keystrokes.

    Hunch AUTO-DETECTS focus-stealing actions (key / click_xy / ref-less type — they use the
    shared keyboard/mouse and require the app frontmost). For those it pops a click-to-approve
    dialog ('Go ahead' / 'Cancel') on the user's screen and only proceeds if they click Go
    ahead — so it never grabs the screen by surprise, and the user approves with ONE CLICK (no
    typing). Focus-free actions run immediately. Pass confirm=true only if the user has already
    approved and you want to skip the dialog.

    When the batch contains ANY focus-stealing action, ALWAYS pass `reason` — one short human
    sentence for WHY you need the screen (e.g. "press Enter to submit the search"). It is shown
    to the user in the focus warning and the approval prompt."""
    blocked = gate.check_focus_steal(_computer, actions, _gate, confirm, reason)
    if blocked:
        return blocked
    return _computer.act(actions)


@mcp.tool()
def screenshot() -> Image:
    """See the screen as a PNG image — for surfaces the tree genuinely can't show (canvas apps, an
    image, a photo/file-preview thumbnail). NOT for reading UI you could get from `snapshot`: if the
    tree looked thin, `click` into the pane and re-`snapshot` instead. A screenshot only shows the
    FRONTMOST app, so in the background it usually shows the user's OTHER app, not your target — and
    it can't be acted on (no refs). Reach for it to VERIFY visual content, not to navigate."""
    return Image(data=base64.b64decode(screenshot_b64()), format="png")


@mcp.tool()
def list_apps() -> str:
    """List the running GUI apps you can target with snapshot(app=...)."""
    return list_running_apps()


@mcp.tool()
def launch_app(name: str, force_accessibility: bool = False, reason: str = "") -> str:
    """Launch or focus an app, then target it (reliable OS call — use instead of
    clicking Dock icons). In simultaneous mode it launches in the BACKGROUND so it
    doesn't steal your view. Set force_accessibility=True for an Electron/Chromium app
    (Spotify, Discord, Slack, Notion, VS Code) whose tree reads empty — it relaunches
    the app so its accessibility tree becomes visible to snapshot.
    Pass `reason` — one short sentence for WHY (shown in the focus-switch warning when
    the launch brings the app to the front)."""
    if not _computer.simultaneous:   # foreground launch = a real focus switch
        blocked = _front_gate(name, reason)
        if blocked:
            return blocked
    set_focus_reason(reason)
    msg = _launch_app(name, force_accessibility, background=_computer.simultaneous)
    _computer.app = name
    refs = _computer.snapshot().count("[e")
    return (f"{msg}; accessibility tree has {refs} elements"
            + ("" if refs > 15 else " (still low — if it's an Electron app, retry with force_accessibility=True)"))


@mcp.tool()
def simultaneous_mode(on: bool = True) -> str:
    """Turn simultaneous mode on/off. When ON, Hunch never steals your cursor/keyboard
    or switches your view: it reads apps WITHOUT bringing them forward, launches apps in
    the background, runs only the focus-free actions (click/select/set-a-field by ref),
    and REFUSES shared-input actions (typed keystrokes, key combos, pixel clicks) that
    would disrupt you. Works for native apps; Electron apps read empty in this mode
    (they need to be frontmost). Turn OFF to let Hunch bring apps forward and use the
    full input set (for when you're away from the machine)."""
    _computer.simultaneous = on
    return ("simultaneous mode ON — Hunch will not touch your foreground, cursor, or keyboard"
            if on else "simultaneous mode OFF — Hunch may bring apps forward and use the cursor/keyboard")


@mcp.tool()
def quit_app(name: str) -> str:
    """Quit an app via the OS — reliable regardless of focus (use instead of ⌘Q,
    which is unreliable on background apps)."""
    return _quit_app(name)


@mcp.tool()
def focus_app(name: str, reason: str = "") -> str:
    """Bring an app to the front (reliable OS call), and target it for snapshots.
    ALWAYS pass `reason` — one short sentence for WHY you're switching the user's view
    (shown to them in the focus warning)."""
    blocked = _front_gate(name, reason)
    if blocked:
        return blocked
    set_focus_reason(reason)
    msg = _focus_app(name)
    _computer.app = name
    return msg


# ── CDP: focus-free control of Chromium browsers + Electron apps ──────────────
_cdp = {"computer": None}


def _close_cdp():
    if _cdp["computer"] is not None:
        try:
            _cdp["computer"].session.close()
        except Exception:
            pass


@mcp.tool()
def web_open(app: str = "Google Chrome", url: str = "", isolated: bool = False) -> str:
    """Open a Chromium BROWSER (or Electron app) for FOCUS-FREE control over CDP — read,
    click, and type in the BACKGROUND without stealing your cursor/keyboard or switching
    your view. This is the way to drive these apps simultaneously with the user (AX can't).
    Then use web_snapshot / web_act.

    `app` is the BROWSER/app name — e.g. "Google Chrome" (the default), "Arc", "Brave",
    "Microsoft Edge", or an Electron app like "Discord"/"Slack"/"Spotify". To open a WEBSITE
    (Gmail, etc.) DON'T pass the site as `app` — keep `app` as the browser and put the site
    in `url` (e.g. web_open("Google Chrome", "https://mail.google.com/...")).

    isolated=True gives a throwaway sandbox profile (no logins). Default uses a persistent,
    DEDICATED Hunch profile — NOT the user's real/default profile (modern Chrome refuses the
    debug port there). If that Hunch profile isn't logged into the site yet, this returns a
    prompt to call web_login once; after that the session persists and stays focus-free."""
    if os.environ.get("HUNCH_FORCE_SANDBOX") == "1":
        isolated = True  # the app's Sandbox toggle forces a throwaway, no-login profile
    from .cdp import CDPComputer
    _close_cdp()
    try:
        _cdp["computer"] = CDPComputer(app, port=9337, url=url or None,
                                       isolated=isolated, background=True)
    except Exception as e:  # e.g. debug port never bound
        return f"couldn't open {app} over CDP: {e}"
    comp = _cdp["computer"]
    if url:
        comp.session.navigate(url)
    comp.session.wait_ready()  # give the page a fair chance to load before judging it
    if not isolated and comp.session.signed_out():
        return (f"opened {app} over CDP, but the Hunch profile isn't signed in "
                f"(now at {comp.session.url()[:70]}). Call web_login(app, url) once to sign in "
                "in a foreground, clearly-marked window — the user types their own credentials, "
                "and afterwards this profile stays logged in and every action is focus-free.")
    snap = comp.snapshot()
    return (f"opened {app} over CDP ({snap.count('[e')} elements visible), focus-free. "
            "Use web_snapshot to look and web_act to click/type/navigate — none of it touches your foreground.")


@mcp.tool()
def web_login(app: str = "Google Chrome", url: str = "") -> str:
    """Sign into the persistent Hunch CDP profile ONCE (there is no way to reuse the user's
    existing browser login over CDP — modern Chrome + site device-bound cookies prevent it).
    `app` is the BROWSER name (default "Google Chrome"); `url` is the site's login/home page
    (e.g. "https://mail.google.com/") — do not pass a website as `app`.
    Opens the browser in the BACKGROUND at that URL (it does NOT steal focus), in a window
    tagged with a green '🟢 HUNCH — LOG IN HERE' banner, and fires a desktop notification.
    The user switches to that window on their own schedule and signs in themselves (Hunch
    never sees the password); the session then persists, and all later web_open / web_act
    calls run focus-free. Tell the user a notification was sent and to switch to the
    banner-tagged window when ready, sign in, leave it open, and confirm when done."""
    from .cdp import CDPComputer, quit_cdp
    _close_cdp()
    quit_cdp(9337)  # fresh window, no stale-instance reuse (does not front anything)
    try:
        _cdp["computer"] = CDPComputer(app, port=9337, url=url or None,
                                       isolated=False, background=True)
    except Exception as e:
        return f"couldn't open {app} for login: {e}"
    comp = _cdp["computer"]
    if url:
        comp.session.navigate(url)
    comp.session.wait_ready()
    comp.session.mark()
    _notify("Switch to the green 'HUNCH — LOG IN HERE' Chrome window to sign in, then tell me when done.",
            "Hunch needs you to sign in")
    return ("Opened a BACKGROUND, banner-tagged Hunch window (did NOT steal focus) and sent a "
            "desktop notification. Tell the user to switch to the window with the green "
            "'🟢 HUNCH — LOG IN HERE' banner when ready (Cmd-Tab to Chrome or click its Dock "
            "icon), sign in, leave it open, and confirm when done. Then use web_open / "
            "web_snapshot / web_act — the login persists and runs focus-free. "
            "(Don't close that window — it kills the debug session.)")


@mcp.tool()
def web_snapshot() -> str:
    """Look at the CDP-controlled browser/Electron page as an accessibility tree
    ([ref] per element). Call web_open first.

    If the tree shows only the NAV/SIDEBAR/header and the main content is missing, the page just
    hasn't rendered it into view yet (lazy-loaded / below the fold / still hydrating). SCROLL and
    re-read: web_act [{"action":"key","key":"PageDown"}] (focus-free via CDP), or wait and
    web_snapshot again. NEVER use the OS `screenshot` tool to see a web page — it captures the
    physical frontmost screen, which for a BACKGROUND CDP window is the user's OWN window, not this
    page. To see the page as pixels focus-free, use `web_screenshot`."""
    if _cdp["computer"] is None:
        return "No web/Electron app open — call web_open(app) first."
    return _cdp["computer"].snapshot()


@mcp.tool()
def web_screenshot() -> Image:
    """PNG of the CDP-controlled page ITSELF (focus-free, via CDP) — for genuinely visual web
    content the tree can't convey (a chart, canvas, image, rendered PDF). Use THIS, never the OS
    `screenshot` tool, for anything in the background browser: the OS one grabs the physical screen
    and would capture the user's own foreground window instead of this page. Call web_open first."""
    if _cdp["computer"] is None:
        return "No web/Electron app open — call web_open(app) first."
    data = _cdp["computer"].session.capture_screenshot()
    if not data:
        return "Could not capture the page."
    return Image(data=base64.b64decode(data), format="png")


@mcp.tool()
def web_act(actions: list) -> str:
    """Execute focus-free page actions on the CDP-controlled app, then return the updated tree.
    Each: {"action":"click","ref":"e12"} | {"action":"type","ref":"e12","text":"hi"} |
    {"action":"key","key":"return"} | {"action":"navigate","url":"https://..."}.
    `type` REPLACES a field's content (no more appending) and selects native <select> dropdown
    options by their visible text (e.g. type "January" into a month picker) — do NOT click a
    native dropdown and hunt for the option; CDP can't open the OS popup, so use `type`.
    To follow a link, CLICK it by ref — do NOT `navigate` to a guessed/constructed URL; only
    navigate to a URL the user gave you or that you read from the page (navigate refuses a host
    that doesn't resolve)."""
    if _cdp["computer"] is None:
        return "No web/Electron app open — call web_open(app) first."
    return _cdp["computer"].act(actions)


@mcp.tool()
def web_restart(app: str = "Google Chrome", url: str = "") -> str:
    """Recover a BROKEN CDP browser by quitting and reopening it fresh. Kills only the Hunch
    CDP instance (never the user's normal browser), relaunches on the SAME persistent profile
    (login kept), reloads `url` if given, and returns the fresh tree.

    LAST RESORT — do NOT restart a page that is merely slow. Restarting discards real
    in-progress loading and network state, so first make sure the page is actually stuck, not
    just taking a while:
      • Wait and re-snapshot (give a heavy page 15–30s). If the tree is CHANGING between
        snapshots, or shows a spinner / skeleton / partial content, it is still LOADING —
        keep waiting, do not restart.
      • Only restart when there's real evidence it's broken: an error page ("Aw, Snap!",
        "This site can't be reached", a network/HTTP error), or a truly blank/empty tree that
        stays unchanged across several snapshots over 20–30s+ with no spinner or progress.
    When unsure, wait and re-snapshot again rather than restarting."""
    from .cdp import CDPComputer, quit_cdp
    _close_cdp()
    quit_cdp(9337)
    try:
        _cdp["computer"] = CDPComputer(app, port=9337, url=url or None,
                                       isolated=False, background=True)
    except Exception as e:
        return f"couldn't reopen {app} over CDP: {e}"
    comp = _cdp["computer"]
    if url:
        comp.session.navigate(url)
    comp.session.wait_ready()
    snap = comp.snapshot()
    return (f"restarted {app} over CDP ({snap.count('[e')} elements). Screen now:\n{snap}")


@mcp.tool()
def web_tabs() -> str:
    """List the open browser tabs/windows in the CDP session — index, title, URL, and which is
    current (*). New tabs opened by a click or form are AUTO-FOLLOWED on the next web_snapshot;
    use web_switch_tab only to override that (go back to a prior tab, or pick a different one)."""
    if _cdp["computer"] is None:
        return "No web/Electron app open — call web_open(app) first."
    tabs = _cdp["computer"].session.tabs()
    if not tabs:
        return "no open tabs"
    return "\n".join(f"[{t['index']}]{'*' if t['current'] else ' '} {t['title']} — {t['url']}" for t in tabs)


@mcp.tool()
def web_switch_tab(index: int) -> str:
    """Switch the CDP session to a specific browser tab by index (see web_tabs), then web_snapshot
    to read it. Use when auto-follow landed on the wrong tab, or to return to an earlier one."""
    if _cdp["computer"] is None:
        return "No web/Electron app open — call web_open(app) first."
    return _cdp["computer"].session.switch_tab(index)


@mcp.tool()
def list_credentials() -> str:
    """List the service NAMES the user has saved credentials for (e.g. "google", "openai").
    Returns names + kind ONLY — never any values. Two kinds: username+password logins
    (use web_fill_login) and single protected values like API keys (use web_fill_secret)."""
    from .creds import list_services, kind_of
    names = list_services()
    if not names:
        return ("No saved credentials. The user can add them on the Hunch app's Credentials page "
                "(Settings). For a one-off sign-in where nothing is saved, use web_login instead.")
    logins = [n for n in names if kind_of(n) != "secret"]
    secrets = [n for n in names if kind_of(n) == "secret"]
    parts = []
    if logins:
        parts.append("logins (web_fill_login): " + ", ".join(logins))
    if secrets:
        parts.append("API keys/secrets (web_fill_secret): " + ", ".join(secrets))
    return "Saved credentials — " + "; ".join(parts)


def _domain_mismatch(service):
    """None if the current CDP page's host is allowed for `service`, else a refusal message
    (the shared guard in gate.py, fed the live CDP page URL)."""
    try:
        url = _cdp["computer"].session.url()
    except Exception:
        url = ""
    return gate.domain_mismatch(service, url)


@mcp.tool()
def web_fill_login(service: str) -> str:
    """Fill the CURRENT CDP page's login form (username + password) using the user's SAVED
    credential for `service`, WITHOUT the values ever entering your context. You pass ONLY the
    service name; Hunch reads the secret from the macOS Keychain and types it straight into the
    page, returning only which fields were filled. Call web_open first and make sure the login
    form is visible. For two-step logins (e.g. Google: email, then password), call once, advance
    with web_act, then call again. After filling, submit via web_act (click the sign-in button or
    press Enter). If the service isn't saved, tell the user to add it on the Credentials page."""
    if _cdp["computer"] is None:
        return "No web/Electron app open — call web_open(app) first."
    from .creds import get_credential, has, kind_of
    if not has(service):
        return (f"No saved credential for '{service}'. Call list_credentials() to see saved "
                "services, or tell the user to add it on the Hunch Credentials page.")
    if kind_of(service) == "secret":
        return (f"'{service}' is a protected value (API key/token), not a login — use "
                f"web_fill_secret('{service}', ref) to type it into a field instead.")
    blocked = _domain_mismatch(service)
    if blocked:
        return blocked
    username, password = get_credential(service)   # stays in this process; never returned
    if not (username or password):
        return f"Couldn't read the '{service}' credential from the Keychain."
    r = _cdp["computer"].session.fill_login(username, password)
    del username, password
    return (f"filled '{service}': username={r['username_filled']}, password={r['password_filled']}. "
            "Now submit via web_act (click the sign-in button or press Enter). If this was the "
            "email step of a two-step login, advance to the password step and call web_fill_login again.")


@mcp.tool()
def web_fill_secret(service: str, ref: str = "") -> str:
    """Type the user's SAVED protected value (API key / token) for `service` into a field on the
    CURRENT CDP page, WITHOUT the value ever entering your context. web_snapshot first and pass the
    target field's `ref`; Hunch reads the secret from the macOS Keychain and types it straight into
    that field, replacing its content — you only learn that it was filled. Omit `ref` to type at
    the currently-focused element. For username+password sign-ins use web_fill_login instead."""
    if _cdp["computer"] is None:
        return "No web/Electron app open — call web_open(app) first."
    from .creds import has, kind_of, get_secret
    if not has(service):
        return (f"No saved credential for '{service}'. Call list_credentials() to see saved "
                "services, or tell the user to add it on the Hunch Credentials page.")
    if kind_of(service) != "secret":
        return (f"'{service}' is a username+password login — use web_fill_login('{service}') "
                "instead.")
    blocked = _domain_mismatch(service)
    if blocked:
        return blocked
    secret = get_secret(service)   # stays in this process; never returned
    if not secret:
        return f"Couldn't read the '{service}' secret from the Keychain."
    _cdp["computer"].session.type_text(ref or None, secret)
    del secret
    return (f"filled the '{service}' secret into {('field ' + ref) if ref else 'the focused element'} "
            "(value not shown to you). Verify the page accepted it and continue.")


@mcp.tool()
def notify_user(message: str) -> str:
    """Alert the user when Hunch needs them SHORTLY — to finish a login, approve a 2FA /
    verification prompt, solve a captcha, or make a decision. Brings the Hunch window back up
    (or fires a desktop notification when running outside the app). Call this the moment you
    hit a step only the human can do, so they don't have to watch the screen."""
    _notify(message, "Hunch needs you")
    return f"notified the user: {message}"


@mcp.tool()
def request_focus(reason: str) -> str:
    """Ask the user's permission BEFORE a focus-stealing step you'll do via OTHER tools (e.g.
    focus_app / launch_app bringing an app to the front). Pops a one-click 'Go ahead / Cancel'
    dialog and returns their choice. You do NOT need this for `act`'s key / click_xy / ref-less
    type — act pops the same dialog automatically. If approved, do the action, then return focus
    to the user's previous app when done."""
    ok = _confirm_dialog(f"Hunch wants to {reason}. Allow it to take over your screen?")
    return ("user clicked Go ahead — proceed, then restore their previous app afterwards"
            if ok else "user did not approve — do NOT proceed with the focus-stealing action")


# ── OS-API layer: focus-free filesystem + clipboard (no UI automation needed) ─────────
_HUNCH_DIR = gate.HUNCH_DIR
_protected = gate.protected


@mcp.tool()
def trash(paths: list) -> str:
    """Move file(s)/folder(s) to the Trash BY PATH — FOCUS-FREE and reversible. Use this to
    delete files instead of driving Finder + ⌘⌫ (which steals focus and is fragile). Accepts
    absolute or ~ paths. Files go to the Trash, so it's recoverable."""
    hit = next((p for p in paths if _protected(p)), None)
    if hit is not None:
        return (f"REFUSED: {hit} is inside ~/.hunch — Hunch's own config/credential metadata. "
                "Not deletable via Hunch; the user can manage it with the `hunch` CLI.")
    return os_ops.trash(paths)


@mcp.tool()
def file_op(op: str, src: str, dst: str = "") -> str:
    """Focus-free filesystem operations by path: op='move' or 'copy' (src -> dst; dst may be a
    folder or a new path), or op='mkdir' (create a folder at src). To delete, use `trash`."""
    for p in (src, dst):
        if p and _protected(p):
            return (f"REFUSED: {p} is inside ~/.hunch — Hunch's own config/credential metadata. "
                    "Not writable via Hunch; the user can manage it with the `hunch` CLI.")
    if op == "move":
        return os_ops.move(src, dst)
    if op == "copy":
        return os_ops.copy(src, dst)
    if op == "mkdir":
        return os_ops.make_dir(src)
    return f"unknown op {op!r} — use 'move', 'copy', or 'mkdir' (or the trash tool to delete)"


@mcp.tool()
def open_file(path: str, app: str = "") -> str:
    """Open a file/folder/URL with its default app (or a named app) — focus-free launch. Also
    opens web URLs and app deep-links (e.g. 'spotify:track:...', 'mailto:...')."""
    return os_ops.open_path(path, app or None)


@mcp.tool()
def reveal_in_finder(paths: list) -> str:
    """Reveal/select item(s) in a Finder window by path. (This one does bring Finder forward —
    it's an explicit 'show me these files' action.)"""
    return os_ops.reveal(paths)


@mcp.tool()
def clipboard_get() -> str:
    """Read the clipboard's text — focus-free (no ⌘C needed)."""
    return os_ops.clipboard_read()


@mcp.tool()
def clipboard_set(text: str) -> str:
    """Put text on the clipboard — focus-free (move data around without ⌘C/⌘V keystrokes)."""
    return os_ops.clipboard_write(text)


_SHELL_AS = gate.SHELL_AS
_DESTRUCTIVE_AS = gate.DESTRUCTIVE_AS


@mcp.tool()
def applescript(script: str, confirm: bool = False) -> str:
    """Run AppleScript to control scriptable native apps FOCUS-FREE via Apple Events — the
    app's own command layer, not its UI/keyboard/focus. Unlocks Mail, Messages, Notes,
    Reminders, Calendar, Music, Finder, Safari, Keynote, etc. (e.g.
    'tell application "Music" to play', or 'tell application "Safari" to get URL of current
    tab of front window'). Returns the script's output or the error.

    SAFETY: read-only queries (get / count) run directly. Scripts that mutate, send, delete,
    quit, or use 'do shell script' pop a one-click 'Go ahead' dialog on the user's screen and
    only run if approved (pass confirm=true to skip the dialog if the user already approved).
    Note: the FIRST time Hunch scripts a given app, macOS shows a one-time 'allow control'
    permission prompt the user must accept."""
    category = gate.applescript_category(script)
    if category and not confirm and _gate.enabled(category):
        preview = script.strip()[:400].replace("\n", "  ").replace("\r", " ")
        if not _confirm_dialog("Hunch wants to run an AppleScript that can change things or "
                               f"control apps:  {preview}   — allow?", screen_approval=False):
            return "User did not approve — AppleScript not run. Ask the user how to proceed."
    ok, out = os_ops.run_applescript(script)
    if ok:
        return out
    return f"AppleScript error: {out[:600]}{gate.applescript_hint(out)}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
