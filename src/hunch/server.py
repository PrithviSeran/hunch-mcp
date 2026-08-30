"""server.py — the Hunch MCP server: drive your REAL Mac over MCP.

Point Claude Desktop at this and you control your machine through your Claude
subscription instead of a metered API key — no per-token spend.

This server is an APP BUILT ON the Hunch SDK — the first one. Every tool below
delegates to one `Hunch` instance through the same dispatch the agent loop uses
(`_dispatch_core`), so there is exactly ONE engine. What makes this server the
*personal* app is nothing but its constructor arguments: policy="personal" (the
live ~/.hunch/config.json resolver incl. the HUNCH_NO_INTERNAL_GATE kill-switch),
default "Hunch" branding, and the legacy storage names (no app_id).

Requires Accessibility permission for whatever process runs this (grant
Claude Desktop in System Settings → Privacy & Security → Accessibility).
"""
import os

from mcp.server.fastmcp import FastMCP, Image

from .playbook import HUNCH_PLAYBOOK
from . import gate
from .notify import notify as _notify_impl


mcp = FastMCP("hunch", instructions=HUNCH_PLAYBOOK)

# When the host owns permissions (e.g. the Hunch app's own approve UX), HUNCH_NO_INTERNAL_GATE=1
# suppresses the duplicate desktop banners here; its gate suppression flows through the
# "personal" policy resolver (env > auto_approve_all > per-category, from ~/.hunch/config.json).
_APP_OWNS_PERMS = bool(os.environ.get("HUNCH_NO_INTERNAL_GATE"))


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


# ── ONE Hunch instance: the engine behind every tool ──────────────────────────────
from .sdk import Hunch  # noqa: E402
from .agent import _dispatch_core  # noqa: E402

_mac = Hunch(
    confirm="dialog",
    policy="personal",        # ~/.hunch/config.json live + HUNCH_NO_INTERNAL_GATE, as always
    check_permissions=False,  # AX problems surface per-tool, not at import
    notify=_notify,           # keeps the app-mode banner suppression
    # HUNCH_SIMULTANEOUS=1 starts the server with the hard focus-free guarantee:
    # no pixel-click fallback, and shared keyboard/cursor actions are refused
    # rather than performed. Previously this could only be turned on by the AGENT
    # calling simultaneous_mode() mid-session, so any embedder — or benchmark —
    # that forgot inherited an agent free to grab the user's cursor. Making it
    # settable at startup lets the operator, not the model, own that promise.
    simultaneous=bool(os.environ.get("HUNCH_SIMULTANEOUS")),
)
_gate = _mac._gate            # single Gate: approval state behaves as the old module globals
_as_str = gate.as_str         # kept aliases (tests + external callers)
_protected = gate.protected
_HUNCH_DIR = gate.HUNCH_DIR


def _run(tool, /, **args):
    """Delegate one MCP tool call to the SDK through the same exception-mapped dispatch
    the agent loop uses. Strings pass through; raw PNG bytes become an MCP Image.
    `tool` is positional-only: several tools (launch_app/quit_app/focus_app) have their
    own `name` argument, which must land in **args without colliding."""
    value, _is_error = _dispatch_core(_mac, tool, args)
    if isinstance(value, bytes):
        return Image(data=value, format="png")
    return value


@mcp.tool()
def snapshot(app: str = "", ref: str = "", max_depth: int | None = None,
             max_nodes: int | None = None, max_children: int | None = None) -> str:
    """See the screen as an accessibility tree. Each element is one line tagged with
    a [ref] like [e12]; you act on elements by ref. Pass an app name to target that
    app's focused window, or leave blank for the frontmost app.

    Truncation is never silent: when the tree is bigger than the walk budget, the
    output ends in `…` marker lines naming what was dropped. Pass ref="e42" to
    re-walk ONLY that element's subtree at full depth (its refs match the full
    snapshot's, and every other ref stays valid) — the way to see inside a
    truncated container, including a long list capped at its sibling limit. `find`
    locates elements without reading a full tree. max_depth/max_nodes/max_children
    override the defaults when you truly need a bigger walk.

    A PARTIAL tree is NOT a dead end. Some apps (chat/mail/master-detail, and Catalyst apps like
    WhatsApp) only expose the pane you're IN — the first snapshot may show just the sidebar/list.
    That does NOT mean the tree is unusable: `click` the item by ref to OPEN it (a chat, an email,
    a conversation), then snapshot AGAIN — the detail pane (compose field, messages, Send) renders
    into the tree once you navigate there. Drive → re-snapshot → drive. Do NOT jump to `screenshot`
    or steal focus (focus_app / launch_app foreground / click_xy) just because the first read was
    thin. Reserve `screenshot` for confirming truly VISUAL content (an image, a file-preview
    thumbnail) — to check what an action did or read UI text, RE-SNAPSHOT, don't screenshot."""
    return _run("snapshot", app=app, ref=ref, max_depth=max_depth, max_nodes=max_nodes,
                max_children=max_children)


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
    return _run("find", role=role, name_contains=name_contains, app=app,
                max_results=max_results)


@mcp.tool()
def act(actions: list, reason: str = "") -> str:
    """Execute one or more UI actions in order (by element ref), then return what
    CHANGED on screen since your last view (~ changed, + new, gone: refs; unchanged
    lines omitted). First look, a window change, or heavy churn returns the full
    tree; call `snapshot` anytime you want the whole tree again. Each action is an object:
      {"action":"click","ref":"e12"}                # press/activate it — OPENS a chat/email/file (focus-free)
      {"action":"right_click","ref":"e12"}          # open its context menu (focus-free)
      {"action":"select","ref":"e12"}               # only HIGHLIGHTS a row — does NOT open it (focus-free)
      {"action":"menu","path":["File","Move to Trash"]}  # invoke a menu-bar command (FOCUS-FREE)
      {"action":"type","ref":"e12","text":"hello"}  # into a ref = focus-free; no ref = types at focus (STEALS FOCUS)
      {"action":"key","key":"return","modifiers":["command"]}   # keystroke (STEALS FOCUS)
      {"action":"window","x":0,"y":0,"w":760,"h":980}  # move/resize the MAIN window (FOCUS-FREE) — how to tile/position
      {"action":"drag","from_ref":"e5","to_x":800,"to_y":400}  # press-move-release (canvas DnD, reorder) — STEALS FOCUS
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
    typing). Focus-free actions run immediately.

    When the batch contains ANY focus-stealing action, ALWAYS pass `reason` — one short human
    sentence for WHY you need the screen (e.g. "press Enter to submit the search"). It is shown
    to the user in the focus warning and the approval prompt."""
    return _run("act", actions=actions, reason=reason)


@mcp.tool()
def screenshot() -> Image:
    """See the screen as a PNG image — for surfaces the tree genuinely can't show (canvas apps, an
    image, a photo/file-preview thumbnail). NOT for reading UI you could get from `snapshot`: if the
    tree looked thin, `click` into the pane and re-`snapshot` instead. A screenshot only shows the
    FRONTMOST app, so in the background it usually shows the user's OTHER app, not your target — and
    it can't be acted on (no refs). Reach for it to VERIFY visual content, not to navigate.
    Pixel coordinates in the image map 1:1 to click_xy (the shot is in point space), so a point you
    read here can be passed straight to a click_xy action."""
    return _run("screenshot")


@mcp.tool()
def list_apps() -> str:
    """List the running GUI apps you can target with snapshot(app=...)."""
    return _run("list_apps")


@mcp.tool()
def launch_app(name: str, force_accessibility: bool = False, reason: str = "") -> str:
    """Launch or focus an app, then target it (reliable OS call — use instead of
    clicking Dock icons). In simultaneous mode it launches in the BACKGROUND so it
    doesn't steal your view. Set force_accessibility=True for an Electron/Chromium app
    (Spotify, Discord, Slack, Notion, VS Code) whose tree reads empty — it relaunches
    the app so its accessibility tree becomes visible to snapshot.
    Pass `reason` — one short sentence for WHY (shown in the focus-switch warning when
    the launch brings the app to the front)."""
    return _run("launch_app", name=name, force_accessibility=force_accessibility,
                reason=reason)


@mcp.tool()
def simultaneous_mode(on: bool = True) -> str:
    """Turn simultaneous mode on/off. When ON, Hunch never steals your cursor/keyboard
    or switches your view: it reads apps WITHOUT bringing them forward, launches apps in
    the background, runs only the focus-free actions (click/select/set-a-field by ref),
    and REFUSES shared-input actions (typed keystrokes, key combos, pixel clicks) that
    would disrupt you. Works for native apps; Electron apps read empty in this mode
    (they need to be frontmost). Turn OFF to let Hunch bring apps forward and use the
    full input set (for when you're away from the machine)."""
    return _run("simultaneous_mode", on=on)


@mcp.tool()
def quit_app(name: str) -> str:
    """Quit an app via the OS — reliable regardless of focus (use instead of ⌘Q,
    which is unreliable on background apps)."""
    return _run("quit_app", name=name)


@mcp.tool()
def focus_app(name: str, reason: str = "") -> str:
    """Bring an app to the front (reliable OS call), and target it for snapshots.
    ALWAYS pass `reason` — one short sentence for WHY you're switching the user's view
    (shown to them in the focus warning)."""
    return _run("focus_app", name=name, reason=reason)


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
    prompt to call web_login once; after that the session persists and stays focus-free.

    CODE EDITORS: pass app="Cursor" / "Visual Studio Code" / "VSCodium" / "Windsurf" and put the
    FOLDER or FILE to open in `url`. This drives a DEDICATED, background Hunch editor window
    (separate from the user's own editor), letting you type into its integrated TERMINAL — which
    the AX tree can read but never write. After opening: web_snapshot, then web_act a 'type' on the
    `[eN] tab "Terminal"` element (a trailing "\\n" runs the command); key ctrl+` opens a terminal
    if none is shown."""
    return _run("web_open", app=app, url=url, isolated=isolated)


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
    return _run("web_login", app=app, url=url)


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
    return _run("web_snapshot")


@mcp.tool()
def web_screenshot() -> Image:
    """PNG of the CDP-controlled page ITSELF (focus-free, via CDP) — for genuinely visual web
    content the tree can't convey (a chart, canvas, image, rendered PDF). Use THIS, never the OS
    `screenshot` tool, for anything in the background browser: the OS one grabs the physical screen
    and would capture the user's own foreground window instead of this page. Call web_open first."""
    return _run("web_screenshot")


@mcp.tool()
def web_act(actions: list) -> str:
    """Execute focus-free page actions on the CDP-controlled app, then return the updated tree.
    Each: {"action":"click","ref":"e12"} | {"action":"type","ref":"e12","text":"hi"} |
    {"action":"click_xy","x":500,"y":250} | {"action":"drag","from_x":10,"from_y":20,
    "to_x":200,"to_y":220} |
    {"action":"key","key":"return"} | {"action":"navigate","url":"https://..."}.
    `type` REPLACES a field's content (no more appending) and selects native <select> dropdown
    options by their visible text (e.g. type "January" into a month picker) — do NOT click a
    native dropdown and hunt for the option; CDP can't open the OS popup, so use `type`.
    For a visual editor whose canvas has no element ref (Google Docs/Slides, drawing tools), call
    web_screenshot, click_xy at a screenshot coordinate, then `type` WITHOUT a ref to type at the
    established focus. These renderer-local actions remain background/focus-free.
    To follow a link, CLICK it by ref — do NOT `navigate` to a guessed/constructed URL; only
    navigate to a URL the user gave you or that you read from the page (navigate refuses a host
    that doesn't resolve)."""
    return _run("web_act", actions=actions)


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
    return _run("web_restart", app=app, url=url)


@mcp.tool()
def web_tabs() -> str:
    """List the open browser tabs/windows in the CDP session — index, title, URL, and which is
    current (*). New tabs opened by a click or form are AUTO-FOLLOWED on the next web_snapshot;
    use web_switch_tab only to override that (go back to a prior tab, or pick a different one)."""
    return _run("web_tabs")


@mcp.tool()
def web_switch_tab(index: int) -> str:
    """Switch the CDP session to a specific browser tab by index (see web_tabs), then web_snapshot
    to read it. Use when auto-follow landed on the wrong tab, or to return to an earlier one."""
    return _run("web_switch_tab", index=index)


@mcp.tool()
def list_credentials() -> str:
    """List the service NAMES the user has saved credentials for (e.g. "google", "openai").
    Returns names + kind ONLY — never any values. Two kinds: username+password logins
    (use web_fill_login) and single protected values like API keys (use web_fill_secret)."""
    return _run("list_credentials")


@mcp.tool()
def web_fill_login(service: str) -> str:
    """Fill the CURRENT CDP page's login form (username + password) using the user's SAVED
    credential for `service`, WITHOUT the values ever entering your context. You pass ONLY the
    service name; Hunch reads the secret from the macOS Keychain and types it straight into the
    page, returning only which fields were filled. Call web_open first and make sure the login
    form is visible. For two-step logins (e.g. Google: email, then password), call once, advance
    with web_act, then call again. After filling, submit via web_act (click the sign-in button or
    press Enter). If the service isn't saved, tell the user to add it on the Credentials page."""
    return _run("web_fill_login", service=service)


@mcp.tool()
def web_fill_secret(service: str, ref: str = "") -> str:
    """Type the user's SAVED protected value (API key / token) for `service` into a field on the
    CURRENT CDP page, WITHOUT the value ever entering your context. web_snapshot first and pass the
    target field's `ref`; Hunch reads the secret from the macOS Keychain and types it straight into
    that field, replacing its content — you only learn that it was filled. Omit `ref` to type at
    the currently-focused element. For username+password sign-ins use web_fill_login instead."""
    return _run("web_fill_secret", service=service, ref=ref)


@mcp.tool()
def notify_user(message: str) -> str:
    """Alert the user when Hunch needs them SHORTLY — to finish a login, approve a 2FA /
    verification prompt, solve a captcha, or make a decision. Brings the Hunch window back up
    (or fires a desktop notification when running outside the app). Call this the moment you
    hit a step only the human can do, so they don't have to watch the screen."""
    return _run("notify_user", message=message)


@mcp.tool()
def request_focus(reason: str) -> str:
    """Ask the user's permission BEFORE a focus-stealing step you'll do via OTHER tools (e.g.
    focus_app / launch_app bringing an app to the front). Pops a one-click 'Go ahead / Cancel'
    dialog and returns their choice. You do NOT need this for `act`'s key / click_xy / ref-less
    type — act pops the same dialog automatically. If approved, do the action, then return focus
    to the user's previous app when done."""
    return _run("request_focus", reason=reason)


@mcp.tool()
def trash(paths: list) -> str:
    """Move file(s)/folder(s) to the Trash BY PATH — FOCUS-FREE and reversible. Use this to
    delete files instead of driving Finder + ⌘⌫ (which steals focus and is fragile). Accepts
    absolute or ~ paths. Files go to the Trash, so it's recoverable."""
    return _run("trash", paths=paths)


@mcp.tool()
def file_op(op: str = "", src: str = "", dst: str = "", batch: list | None = None) -> str:
    """Focus-free filesystem operations by path: op='move' or 'copy' (src -> dst; dst may be a
    folder or a new path), or op='mkdir' (create a folder at src). For MULTIPLE operations pass
    batch=[{"op","src","dst"},...] and they all run in ONE call — sorting a folder is one call,
    not one per file. To delete, use `trash`."""
    return _run("file_op", op=op, src=src, dst=dst, batch=batch)


@mcp.tool()
def open_file(path: str, app: str = "") -> str:
    """Open a file/folder/URL with its default app (or a named app) — focus-free launch. Also
    opens web URLs and app deep-links (e.g. 'spotify:track:...', 'mailto:...')."""
    return _run("open_file", path=path, app=app)


@mcp.tool()
def reveal_in_finder(paths: list) -> str:
    """Reveal/select item(s) in a Finder window by path. (This one does bring Finder forward —
    it's an explicit 'show me these files' action.)"""
    return _run("reveal_in_finder", paths=paths)


@mcp.tool()
def clipboard_get() -> str:
    """Read the clipboard's text — focus-free (no ⌘C needed)."""
    return _run("clipboard_get")


@mcp.tool()
def clipboard_set(text: str) -> str:
    """Put text on the clipboard — focus-free (move data around without ⌘C/⌘V keystrokes)."""
    return _run("clipboard_set", text=text)


@mcp.tool()
def applescript(script: str) -> str:
    """Run AppleScript to control scriptable native apps FOCUS-FREE via Apple Events — the
    app's own command layer, not its UI/keyboard/focus. Unlocks Mail, Messages, Notes,
    Reminders, Calendar, Music, Finder, Safari, Keynote, etc. (e.g.
    'tell application "Music" to play', or 'tell application "Safari" to get URL of current
    tab of front window'). Returns the script's output or the error.

    FOCUS RULE: never use 'activate', and never create windows the task doesn't need
    (e.g. Mail drafts: omit 'visible:true' — a visible compose window drags Mail frontmost).
    Apple Events work with the app in the background; keep it there.

    SAFETY: read-only queries (get / count) run directly. Scripts that mutate, send, delete,
    quit, or use 'do shell script' pop a one-click 'Go ahead' dialog on the user's screen and
    only run if approved. Note: the FIRST time Hunch scripts a given app, macOS shows a
    one-time 'allow control' permission prompt the user must accept."""
    return _run("applescript", script=script)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
