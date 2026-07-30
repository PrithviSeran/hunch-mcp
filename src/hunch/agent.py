"""agent.py — the Hunch agent loop: an LLM drives this Mac through the Hunch
primitives, on the user's own machine and logged-in apps.

    from hunch import Hunch
    mac = Hunch()
    result = mac.agent.run("reply to Sarah's latest email, but don't send it")
    print(result.text)

Claude reads the Hunch playbook as its system prompt and drives the Mac through the same
tools the MCP server exposes. TWO interchangeable backends, same tools and gates:

  backend="api"          — the `anthropic` SDK on a metered ANTHROPIC_API_KEY.
  backend="subscription" — `claude-agent-sdk` on the user's Claude subscription
                           (the sign-in from hunch.login() / Claude Code; no per-token
                           cost). Auth is EXPLICIT: hunch.auth.resolve() is the single
                           resolution order, surfaced by hunch.auth.status().
  backend="auto" (default) — api if ANTHROPIC_API_KEY is set, else subscription if
                           signed in, else a HunchError naming both fixes.

Both SDKs are OPTIONAL dependencies (pip install 'hunch-sdk[agent]' brings both),
imported lazily so plain `import hunch` stays clean. Other models can use the instance
SDK primitives directly in their own harness.
"""
import base64
import os
from dataclasses import dataclass, field

from .playbook import HUNCH_PLAYBOOK
from .gate import HunchError, ApprovalDenied, AccessibilityNotGranted, WebNotOpen

__all__ = ["Agent", "AgentResult"]

DEFAULT_MODEL = "claude-opus-4-8"

# ── tool schemas ──────────────────────────────────────────────────────────────
# The full-input action item (native AX layer). Shared by the `act` tool.
_ACTION_ITEM = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["click", "right_click", "select", "type", "menu", "key", "window", "drag", "click_xy"]},
        "ref": {"type": "string"}, "text": {"type": "string"},
        "path": {"type": "array", "items": {"type": "string"}},
        "key": {"type": "string"}, "modifiers": {"type": "array", "items": {"type": "string"}},
        "x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer"}, "h": {"type": "integer"},
        "app": {"type": "string"},
        "from_ref": {"type": "string"}, "to_ref": {"type": "string"},
        "from_x": {"type": "integer"}, "from_y": {"type": "integer"},
        "to_x": {"type": "integer"}, "to_y": {"type": "integer"}},
    "required": ["action"]}

# The web/CDP action item — a smaller verb set (no menu/pixel; navigate instead).
_WEB_ACTION_ITEM = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["click", "type", "key", "navigate"]},
        "ref": {"type": "string"}, "text": {"type": "string"},
        "key": {"type": "string"}, "modifiers": {"type": "array", "items": {"type": "string"}},
        "url": {"type": "string"}},
    "required": ["action"]}


def _obj(props=None, required=None):
    return {"type": "object", "properties": props or {}, **({"required": required} if required else {})}


# Names identical to the MCP tools so every HUNCH_PLAYBOOK reference resolves. Descriptions
# are condensed (the deep procedural guidance lives in the playbook system prompt); each keeps
# the prescriptive when-to-call sentence, which measurably lifts should-call rate on Opus.
AGENT_TOOLS = [
    {"name": "snapshot",
     "description": ("See an app as an accessibility tree ([ref] per element) — your primary way "
                     "to read UI focus-free. Pass an app name or leave blank for the frontmost. "
                     "ref='e42' re-walks just that element's subtree; truncated trees end in `…` "
                     "markers naming what was dropped."),
     "input_schema": _obj({"app": {"type": "string"}, "ref": {"type": "string"},
                           "max_depth": {"type": "integer"}, "max_nodes": {"type": "integer"},
                           "max_children": {"type": "integer"}})},
    {"name": "find",
     "description": ("Search an app's WHOLE tree for matching elements without reading a full "
                     "snapshot — the cheap way to locate one control in a big window. Filter by "
                     "role (e.g. 'button', case-insensitive) and/or name_contains (substring of "
                     "title/description/value). Returns actable [ref]s."),
     "input_schema": _obj({"role": {"type": "string"}, "name_contains": {"type": "string"},
                           "app": {"type": "string"}, "max_results": {"type": "integer"}})},
    {"name": "act",
     "description": ("Run UI actions in order by ref, then get what CHANGED on screen (unchanged lines omitted; snapshot gives the full tree). To tile/position a window use the 'window' verb (x/y/w/h, focus-free, targets the main window; pass `app` to target a specific app — to tile TWO apps give each window action its own `app`, e.g. app:'TextEdit' x:0 then app:'Notes' x:756). Verbs: click "
                     "(activate), right_click (context menu), select (highlight a row), type (set "
                     "a field by ref = focus-free; no ref types at focus = STEALS FOCUS), menu "
                     "(invoke a menu-bar path, e.g. ['File','Move to Trash'] — the focus-free "
                     "stand-in for ⌘-shortcuts), key/click_xy (STEAL FOCUS, last resort). Prefer "
                     "the focus-free verbs. Pass `reason` when any action steals focus."),
     "input_schema": _obj({"actions": {"type": "array", "items": _ACTION_ITEM},
                           "reason": {"type": "string"}}, ["actions"])},
    {"name": "screenshot",
     "description": ("See the frontmost app as a PNG — only for genuinely visual content the tree "
                     "can't convey (an image, a chart). To read UI text or check an action, "
                     "re-snapshot instead. Image pixels are in point space, so a coordinate read "
                     "here can go straight to a click_xy action."),
     "input_schema": _obj()},
    {"name": "list_apps", "description": "List the running GUI apps you can target with snapshot.",
     "input_schema": _obj()},
    {"name": "launch_app",
     "description": ("Launch or focus an app (reliable OS call — beats clicking the Dock). Set "
                     "force_accessibility=true for an Electron/Chromium app whose tree reads empty. "
                     "In simultaneous mode it launches in the background."),
     "input_schema": _obj({"name": {"type": "string"},
                           "force_accessibility": {"type": "boolean"},
                           "reason": {"type": "string"}}, ["name"])},
    {"name": "simultaneous_mode",
     "description": ("Toggle simultaneous mode. ON = never steal the user's cursor/keyboard/view "
                     "(background reads, focus-free actions only, shared-input actions refused). "
                     "OFF = may bring apps forward and use the full input set."),
     "input_schema": _obj({"on": {"type": "boolean"}})},
    {"name": "quit_app", "description": "Quit an app via the OS (reliable regardless of focus).",
     "input_schema": _obj({"name": {"type": "string"}}, ["name"])},
    {"name": "focus_app",
     "description": ("Bring an app to the front and target it. This switches the user's view — "
                     "always pass `reason` (one short sentence for why)."),
     "input_schema": _obj({"name": {"type": "string"}, "reason": {"type": "string"}}, ["name"])},
    {"name": "web_open",
     "description": ("Open a Chromium browser or Electron app for FOCUS-FREE control over CDP — "
                     "the way to drive web/Electron apps in the background. `app` is the browser "
                     "(default 'Google Chrome'); put a website in `url`. Uses the persistent Hunch "
                     "profile; call web_login once if it isn't signed in. CODE EDITORS: app="
                     "'Cursor'/'Visual Studio Code'/'VSCodium'/'Windsurf' with the FOLDER/FILE in "
                     "`url` opens a dedicated background editor window whose integrated TERMINAL you "
                     "can type into (AX can't write it) — snapshot, then web_act 'type' on the "
                     "'Terminal' tab (trailing newline runs the command); key ctrl+` opens one."),
     "input_schema": _obj({"app": {"type": "string"}, "url": {"type": "string"},
                           "isolated": {"type": "boolean"}})},
    {"name": "web_login",
     "description": ("Open a background, banner-tagged window for the HUMAN to sign in once (Hunch "
                     "never sees the password); the login then persists. Fires a desktop notification."),
     "input_schema": _obj({"app": {"type": "string"}, "url": {"type": "string"}})},
    {"name": "web_snapshot",
     "description": "Read the CDP-controlled page as an accessibility tree. Call web_open first.",
     "input_schema": _obj()},
    {"name": "web_screenshot",
     "description": ("PNG of the CDP page itself (focus-free) — for visual web content the tree "
                     "can't convey. Use this, never the OS screenshot, for the background browser."),
     "input_schema": _obj()},
    {"name": "web_act",
     "description": ("Run focus-free page actions, then get the updated tree. Verbs: click (by ref), "
                     "type (REPLACES a field; picks <select> options by visible text), key, navigate "
                     "(only to a URL you were given or read from the page — click links, don't guess)."),
     "input_schema": _obj({"actions": {"type": "array", "items": _WEB_ACTION_ITEM}}, ["actions"])},
    {"name": "web_restart",
     "description": ("Recover a BROKEN CDP browser by quitting and reopening it fresh (login kept). "
                     "Last resort — don't restart a merely-slow page; wait and re-snapshot first."),
     "input_schema": _obj({"app": {"type": "string"}, "url": {"type": "string"}})},
    {"name": "web_tabs",
     "description": "List the open CDP tabs (index, title, URL, which is current).",
     "input_schema": _obj()},
    {"name": "web_switch_tab",
     "description": "Switch the CDP session to a tab by index (see web_tabs), then web_snapshot.",
     "input_schema": _obj({"index": {"type": "integer"}}, ["index"])},
    {"name": "list_credentials",
     "description": ("List the service NAMES the user saved credentials for (names + kind only, "
                     "never values). Fill them with web_fill_login / web_fill_secret."),
     "input_schema": _obj()},
    {"name": "web_fill_login",
     "description": ("Fill the current CDP page's login form from the user's saved credential for "
                     "`service`, WITHOUT the values entering your context — you only learn which "
                     "fields were filled. Then submit via web_act. web_open first."),
     "input_schema": _obj({"service": {"type": "string"}}, ["service"])},
    {"name": "web_fill_secret",
     "description": ("Type the user's saved protected value (API key/token) for `service` into a "
                     "field on the current CDP page, WITHOUT the value entering your context. "
                     "web_snapshot first and pass the field's ref."),
     "input_schema": _obj({"service": {"type": "string"}, "ref": {"type": "string"}}, ["service"])},
    {"name": "notify_user",
     "description": ("Alert the user when you need them SHORTLY — to finish a login, approve a 2FA "
                     "prompt, solve a captcha, or make a decision only they can. Call this the moment "
                     "you hit a step only the human can do."),
     "input_schema": _obj({"message": {"type": "string"}}, ["message"])},
    {"name": "request_focus",
     "description": ("Ask the user's permission BEFORE a focus-stealing step you'll do via other "
                     "tools. Pops a one-click Go ahead / Cancel dialog and returns their choice."),
     "input_schema": _obj({"reason": {"type": "string"}}, ["reason"])},
    {"name": "trash",
     "description": ("Move file(s)/folder(s) to the Trash by path — focus-free and reversible. Use "
                     "this to delete files instead of driving Finder."),
     "input_schema": _obj({"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"])},
    {"name": "file_op",
     "description": ("Focus-free filesystem ops by path: op='move'|'copy' (src -> dst) or op='mkdir' "
                     "(create a folder at src). For MULTIPLE operations pass batch=[{op,src,dst},...] "
                     "in ONE call (e.g. sort a whole folder at once). To delete, use trash."),
     "input_schema": _obj({"op": {"type": "string", "enum": ["move", "copy", "mkdir"]},
                           "src": {"type": "string"}, "dst": {"type": "string"},
                           "batch": {"type": "array", "items": {
                               "type": "object", "properties": {
                                   "op": {"type": "string", "enum": ["move", "copy", "mkdir"]},
                                   "src": {"type": "string"}, "dst": {"type": "string"}},
                               "required": ["op", "src"]}}})},
    {"name": "open_file",
     "description": ("Open a file/folder/URL/app-deep-link with its default (or a named) app — "
                     "focus-free launch (also 'mailto:', 'spotify:track:...')."),
     "input_schema": _obj({"path": {"type": "string"}, "app": {"type": "string"}}, ["path"])},
    {"name": "reveal_in_finder",
     "description": "Reveal/select item(s) in a Finder window by path (this does front Finder).",
     "input_schema": _obj({"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"])},
    {"name": "clipboard_get", "description": "Read the clipboard's text — focus-free (no ⌘C).",
     "input_schema": _obj()},
    {"name": "clipboard_set", "description": "Put text on the clipboard — focus-free (no ⌘V).",
     "input_schema": _obj({"text": {"type": "string"}}, ["text"])},
    {"name": "applescript",
     "description": ("Run AppleScript to control scriptable native apps FOCUS-FREE via Apple Events "
                     "— Mail, Messages, Notes, Reminders, Calendar, Music, Finder, Safari. Prefer "
                     "this over UI-driving those apps. Risky scripts prompt the user."),
     "input_schema": _obj({"script": {"type": "string"}}, ["script"])},
]

AGENT_ADDENDUM = """\

── You are an autonomous agent driving THIS Mac ──
You are driving the user's real Mac on their behalf through the tools above. The playbook
above is your operating manual — follow its NATIVE-FIRST routing and layer order.

TERMINATION: when the task is complete — or you are blocked on something only the human can
do (a login, a 2FA prompt, a decision) AFTER calling notify_user — end your turn with a
plain final message summarizing the outcome. There is no "done" tool; your final message is
the deliverable. Do not end mid-task with only a statement of intent ("I'll now…") — do the
action, then report.

REFUSALS: a tool result starting with "REFUSED" or "User did NOT approve" is a hard human
decision, not a transient error. Do NOT retry the identical call — adapt (a focus-free
alternative) or call notify_user and stop.

CAUTION: before an irreversible or outward action (sending a message/email, deleting beyond
the Trash, a purchase), confirm intent with the user via notify_user unless they already
asked for exactly that. Report outcomes faithfully — if something failed, say so."""


# ── exception → tool_result content mapping ─────────────────────────────────────
def _img(png_bytes):
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png_bytes).decode()}}]


# Dispatcher: tool name -> callable(mac, args) -> content (str, or raw PNG bytes for the
# screenshot tools — each backend formats bytes into its own image-block shape).
_DISPATCH = {
    "snapshot": lambda m, a: m.snapshot(a.get("app", ""), ref=a.get("ref") or None,
                                        max_depth=a.get("max_depth"), max_nodes=a.get("max_nodes"),
                                        max_children=a.get("max_children")),
    "find": lambda m, a: m.find(role=a.get("role") or None, name_contains=a.get("name_contains") or None,
                                app=a.get("app", ""), max_results=a.get("max_results", 20)),
    # `confirm` isn't in the agent tool schema (an agent must not self-approve); the MCP
    # server's tools DO pass it through when the human already approved out-of-band.
    "act": lambda m, a: m.act(a.get("actions", []), reason=a.get("reason", ""),
                              confirm=a.get("confirm", False)),
    "screenshot": lambda m, a: m.screenshot(),
    "list_apps": lambda m, a: m.list_apps(),
    "launch_app": lambda m, a: m.launch_app(a["name"], force_accessibility=a.get("force_accessibility", False),
                                            reason=a.get("reason", "")),
    "simultaneous_mode": lambda m, a: _set_simultaneous(m, a.get("on", True)),
    "quit_app": lambda m, a: m.quit_app(a["name"]),
    "focus_app": lambda m, a: m.focus_app(a["name"], reason=a.get("reason", "")),
    "web_open": lambda m, a: m.web.open(url=a.get("url", ""), app=a.get("app", "Google Chrome"),
                                        isolated=a.get("isolated", False)),
    "web_login": lambda m, a: m.web.login(url=a.get("url", ""), app=a.get("app", "Google Chrome")),
    "web_snapshot": lambda m, a: m.web.snapshot(),
    "web_screenshot": lambda m, a: m.web.screenshot(),
    "web_act": lambda m, a: m.web.act(a.get("actions", [])),
    "web_restart": lambda m, a: m.web.restart(url=a.get("url", ""), app=a.get("app", "Google Chrome")),
    "web_tabs": lambda m, a: m.web.tabs(),
    "web_switch_tab": lambda m, a: m.web.switch_tab(a["index"]),
    "list_credentials": lambda m, a: m.list_credentials(),
    "web_fill_login": lambda m, a: m.web.fill_login(a["service"]),
    "web_fill_secret": lambda m, a: m.web.fill_secret(a["service"], ref=a.get("ref", "")),
    "notify_user": lambda m, a: _notify(m, a["message"]),
    "request_focus": lambda m, a: _request_focus(m, a["reason"]),
    "trash": lambda m, a: m.files.trash(a.get("paths", [])),
    "file_op": lambda m, a: _file_op(m, a),
    "open_file": lambda m, a: m.files.open(a["path"], app=a.get("app") or None),
    "reveal_in_finder": lambda m, a: m.files.reveal(a.get("paths", [])),
    "clipboard_get": lambda m, a: m.clipboard.get(),
    "clipboard_set": lambda m, a: m.clipboard.set(a["text"]),
    "applescript": lambda m, a: m.applescript(a["script"], confirm=a.get("confirm", False)),
}


def _set_simultaneous(mac, on):
    mac.simultaneous = bool(on)
    return ("simultaneous mode ON — Hunch will not touch your foreground, cursor, or keyboard"
            if on else "simultaneous mode OFF — Hunch may bring apps forward and use the cursor/keyboard")


def _notify(mac, message):
    mac.notify(message, f"{getattr(mac, 'app_name', 'Hunch')} needs you")
    return f"notified the user: {message}"


def _request_focus(mac, reason):
    name = getattr(mac, "app_name", "Hunch")
    ok = mac._gate.confirm_dialog(f"{name} wants to {reason}. Allow it to take over your screen?",
                                  category="focus", detail=reason)
    return ("user clicked Go ahead — proceed, then restore their previous app afterwards"
            if ok else "user did not approve — do NOT proceed with the focus-stealing action")


def _file_op(mac, a):
    def one(op, src, dst):
        if op == "move":
            return mac.files.move(src, dst)
        if op == "copy":
            return mac.files.copy(src, dst)
        if op == "mkdir":
            return mac.files.mkdir(src)
        return f"unknown op {op!r} — use 'move', 'copy', or 'mkdir' (or the trash tool to delete)"
    batch = a.get("batch")
    if batch:
        # whole file jobs in ONE call (a folder sort is one tool call, not one per file);
        # keeps going on per-item failures and reports each line, like trash()
        return "\n".join(one(i.get("op"), i.get("src", ""), i.get("dst", "")) for i in batch)
    return one(a.get("op"), a.get("src", ""), a.get("dst", ""))


def _dispatch_core(mac, name, args):
    """Shared tool execution for BOTH backends: run one tool -> (value, is_error) where
    value is str (text) or bytes (raw PNG). Expected Hunch exceptions become plain content
    strings so the model can adapt; only genuinely unexpected exceptions set is_error
    (mirrors the MCP server's return-the-message behavior)."""
    from .local_mac import StaleRef
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"unknown tool {name}", True
    try:
        return fn(mac, args or {}), False
    except ApprovalDenied as e:
        return (f"REFUSED: {e} — the user declined; do not retry the identical action, "
                "adapt or call notify_user"), False
    except StaleRef:
        return "ref is stale — re-snapshot the app and use fresh refs", False
    except (WebNotOpen, AccessibilityNotGranted, HunchError) as e:
        return str(e), False
    except Exception as e:  # noqa: BLE001 — genuinely unexpected: flag it
        return f"error: {e}", True


def _run_tool(mac, tool_use):
    """API-backend formatter: dispatch one tool_use block -> an anthropic tool_result dict
    (bytes become an anthropic-shaped image block)."""
    value, is_error = _dispatch_core(mac, tool_use.name, tool_use.input or {})
    content = _img(value) if isinstance(value, bytes) else value
    out = {"type": "tool_result", "tool_use_id": tool_use.id, "content": content}
    if is_error:
        out["is_error"] = True
    return out


def _preview(content):
    if isinstance(content, list):
        texts = [d.get("text", "") for d in content
                 if isinstance(d, dict) and d.get("type") == "text"]
        s = " ".join(t for t in texts if t).strip()
        if not s:
            return "[image]"
    else:
        s = str(content)
    return s if len(s) <= 200 else s[:200] + "…"


# ── caching: rolling breakpoint on the last user-message block ──────────────────
def _mark_cache(messages):
    """Put a cache_control breakpoint on the last block of the most recent user message and
    clear it from earlier ones. Only touches dict blocks (our tool_result / text dicts) — it
    NEVER mutates the pydantic assistant/thinking blocks we replay verbatim."""
    last = None
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block.pop("cache_control", None)
                if msg.get("role") == "user":
                    last = block
    if last is not None:
        last["cache_control"] = {"type": "ephemeral"}


# ── subscription backend (claude-agent-sdk — the user's Claude sign-in, no API key) ──

def _mcp_content(value, is_error):
    """Format a _dispatch_core result into MCP tool-output shape (the subscription
    backend's wire format — note: NOT the anthropic image-block shape _img builds)."""
    if isinstance(value, bytes):
        content = [{"type": "image", "data": base64.b64encode(value).decode(),
                    "mimeType": "image/png"}]
    else:
        content = [{"type": "text", "text": str(value)}]
    return {"content": content, **({"is_error": True} if is_error else {})}


def _sdk_tools(mac):
    """One in-process MCP tool per AGENT_TOOLS entry, driving the SAME Hunch instance
    through _dispatch_core — no subprocess, no second server, same gates."""
    import asyncio
    from claude_agent_sdk import tool

    def _make(name):
        async def handler(args):
            loop = asyncio.get_running_loop()   # Hunch calls block on AX/subprocess -> executor
            value, is_error = await loop.run_in_executor(None, _dispatch_core, mac, name, args or {})
            return _mcp_content(value, is_error)
        return handler

    return [tool(t["name"], t["description"], t["input_schema"])(_make(t["name"]))
            for t in AGENT_TOOLS]


class _SubscriptionRunner:
    """The subscription backend: claude-agent-sdk on the user's Claude sign-in
    (hunch.login() / Claude Code / CLAUDE_CODE_OAUTH_TOKEN — see hunch.auth).

    Sync facade over the SDK's asyncio client: a daemon event-loop thread plus
    run_coroutine_threadsafe, so Agent.run() stays synchronous. The connected client
    is kept across run() calls (continuation) while the options are unchanged."""

    def __init__(self, hunch, auth=None, app_id=None, can_use_tool=None):
        self._h = hunch
        self._auth = auth              # OAuthToken (injected) or None (ambient)
        self._app_id = app_id
        self._permit = can_use_tool    # host-owned permission callback (see Agent.__init__)
        self._loop = None
        self._thread = None
        self._client = None
        self._opts_key = None

    # — event-loop plumbing —
    def _call(self, coro):
        import asyncio
        if self._loop is None:
            import threading
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever,
                                            daemon=True, name="hunch-agent-sdk")
            self._thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # — auth: what (if anything) to hand the CLI subprocess. NEVER touches os.environ;
    # the token rides in the per-client options.env (merged over the inherited env). —
    def _cli_env(self):
        from . import auth as auth_mod
        from .auth import OAuthToken
        if isinstance(self._auth, OAuthToken):
            return {"CLAUDE_CODE_OAUTH_TOKEN": self._auth.value}
        if not auth_mod.subscription_available(self._app_id):
            raise HunchError("not signed in — call hunch.login() to use your Claude "
                             "subscription, or set ANTHROPIC_API_KEY for the metered API "
                             "backend")
        token = auth_mod.subscription_token(self._app_id)
        return {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {}

    # — options —
    def _options(self, sdk, model, max_turns, system_suffix, effort, env):
        async def _allow_hunch_only(tool_name, input_data, context):
            if tool_name.startswith("mcp__hunch__"):
                return sdk.PermissionResultAllow()
            return sdk.PermissionResultDeny(   # no built-in Read/Bash/Write — Hunch tools only
                message="the Hunch agent only runs Hunch tools")

        system = HUNCH_PLAYBOOK + "\n" + AGENT_ADDENDUM
        if system_suffix:
            system += "\n" + system_suffix
        opts = dict(
            system_prompt=system,
            mcp_servers={"hunch": sdk.create_sdk_mcp_server("hunch", tools=_sdk_tools(self._h))},
            permission_mode="default",         # unmatched tools -> can_use_tool (our policy point)
            setting_sources=[],                # ignore the user's .claude / .mcp.json entirely
            model=model,                       # None -> the subscription's default model
            max_turns=max_turns,
            effort=effort,
            env=env,                           # {} or the injected/namespaced OAuth token
            max_buffer_size=16 * 1024 * 1024,  # snapshots/screenshots exceed the 1MB default
        )
        if self._permit is not None:
            # A host (e.g. the Hunch app) owns approval: its callback governs EVERY tool, so we
            # don't restrict allowed_tools — built-in Read/Grep/etc. reach it too, exactly like a
            # raw claude-agent-sdk can_use_tool. The callback must match that (tool_name, input,
            # context) -> PermissionResultAllow/Deny protocol.
            opts["can_use_tool"] = self._permit
        else:
            opts["can_use_tool"] = _allow_hunch_only
            opts["allowed_tools"] = [f"mcp__hunch__{t['name']}" for t in AGENT_TOOLS]
        return sdk.ClaudeAgentOptions(**opts)

    # — the loop —
    async def _run_async(self, client, task, emit):
        await client.query(task)
        final_text, turns, stop_reason, usage, aborted = "", 0, "end_turn", {}, True
        async for msg in client.receive_response():   # duck-typed: no SDK types needed in tests
            kind = type(msg).__name__
            if kind == "AssistantMessage":
                for b in msg.content:
                    bname = type(b).__name__
                    if bname == "TextBlock" and b.text.strip():
                        emit("text", b.text.strip())
                    elif bname == "ToolUseBlock":
                        emit("tool", {"name": b.name.split("__")[-1], "input": b.input})
            elif kind == "UserMessage":
                for b in (msg.content if isinstance(msg.content, list) else []):
                    if type(b).__name__ == "ToolResultBlock":
                        emit("tool_result", _preview(b.content))
            elif kind == "ResultMessage":
                final_text = msg.result or ""
                turns = msg.num_turns or 0
                aborted = bool(msg.is_error)
                stop_reason = msg.stop_reason or ("error" if aborted else "end_turn")
                usage = msg.usage or {}
                if aborted:
                    emit("error", f"stopped: {msg.subtype}")
                else:
                    emit("done", final_text)
        return AgentResult(text=final_text, turns=turns, stop_reason=stop_reason,
                           usage=usage, aborted=aborted)

    def run(self, task, model=None, max_turns=40, on_event=None, system_suffix="", effort=None):
        try:
            import claude_agent_sdk as sdk
        except ImportError as e:
            raise HunchError("the subscription backend needs the optional 'claude-agent-sdk' "
                             "package — install it with: pip install 'hunch-sdk[agent]'") from e
        env = self._cli_env()            # injected token, namespaced slot, or {} (CLI self-auth)

        key = (model, max_turns, system_suffix, effort)
        if self._client is not None and key != self._opts_key:
            self.reset()                 # options changed -> fresh conversation
        if self._client is None:
            client = sdk.ClaudeSDKClient(
                options=self._options(sdk, model, max_turns, system_suffix, effort, env))
            self._call(client.connect())
            self._client, self._opts_key = client, key
        emit = on_event or (lambda kind, data: None)
        return self._call(self._run_async(self._client, task, emit))

    def interrupt(self):
        """Interrupt the in-flight turn (the SDK's own interrupt) WITHOUT tearing down the
        conversation — the next run() continues the same context. Called from another thread
        (e.g. a Stop button) while run() blocks on the loop; a no-op if nothing is connected."""
        if self._client is None or self._loop is None:
            return
        import asyncio
        try:
            asyncio.run_coroutine_threadsafe(self._client.interrupt(), self._loop).result(timeout=5)
        except Exception:
            pass

    def reset(self):
        if self._client is not None:
            client, self._client, self._opts_key = self._client, None, None
            try:
                self._call(client.disconnect())
            except Exception:
                pass

    def close(self):
        self.reset()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()
            self._loop = self._thread = None


@dataclass
class AgentResult:
    text: str
    turns: int
    stop_reason: str
    usage: dict = field(default_factory=dict)
    aborted: bool = False


class Agent:
    """The agent loop. Created lazily as `Hunch.agent`. Holds conversation state across
    run() calls (continuation); call reset() to start a fresh task."""

    def __init__(self, hunch, client=None, backend="auto", auth=None, can_use_tool=None):
        from .auth import ApiKey, OAuthToken
        self._h = hunch
        self._client = client          # test seam; None -> lazily anthropic.Anthropic()
        self.backend = backend         # "auto" | "api" | "subscription" (per-run override too)
        self.auth = auth               # None (ambient) | ApiKey | OAuthToken | "none"
        # Optional host-owned permission callback. When set, the subscription backend hands EVERY
        # tool to it — a raw claude-agent-sdk can_use_tool(tool_name, input, context) ->
        # PermissionResultAllow/Deny — instead of auto-allowing Hunch tools. This is how the Hunch
        # app routes each tool through its own Approve/Deny UI. (Subscription backend only.)
        self._can_use_tool = can_use_tool
        self.messages = []
        self._abort = False            # api backend: between-turns cancel flag (see interrupt())
        self._sub = None               # _SubscriptionRunner, built on first subscription run
        if not (auth is None or auth == "none" or isinstance(auth, (ApiKey, OAuthToken))):
            raise HunchError(f"unknown auth {auth!r} — pass hunch.ApiKey(...), "
                             "hunch.OAuthToken(...), 'none', or None (ambient)")
        implied = self._implied_backend()
        if implied and backend not in ("auto", implied):
            raise HunchError(f"auth={type(auth).__name__}(...) implies the {implied!r} backend, "
                             f"but backend={backend!r} was requested")

    @property
    def _app_id(self):
        return getattr(self._h, "_app_id", None)   # set by Hunch when namespaced

    def _implied_backend(self):
        from .auth import ApiKey, OAuthToken
        if isinstance(self.auth, ApiKey):
            return "api"
        if isinstance(self.auth, OAuthToken):
            return "subscription"
        return None

    def _resolve_backend(self, backend):
        """Injected credentials imply their backend; an explicit choice wins otherwise;
        "auto" picks by ambient credentials — the SAME order hunch.auth.status() reports.
        auth="none" means never scavenge: with no injected credential, raise."""
        implied = self._implied_backend()
        b = backend or implied or self.backend or "auto"
        if implied and b != implied:
            raise HunchError(f"auth={type(self.auth).__name__}(...) implies the {implied!r} "
                             f"backend, but backend={b!r} was requested")
        if self.auth == "none" and implied is None:
            raise HunchError("auth='none' — ambient credentials are disabled for this instance; "
                             "pass hunch.ApiKey(...) or hunch.OAuthToken(...) (or auth=None "
                             "to allow ambient resolution)")
        if b in ("api", "subscription"):
            return b
        if b != "auto":
            raise HunchError(f"unknown backend {b!r} — use 'api', 'subscription', or 'auto'")
        if self._client is not None:       # injected client (test seam) is an api client
            return "api"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "api"
        from . import auth
        try:
            import claude_agent_sdk  # noqa: F401
            have_sdk = True
        except ImportError:
            have_sdk = False
        if have_sdk and auth.subscription_available(self._app_id):
            return "subscription"
        raise HunchError(
            "no credentials for the agent loop — call hunch.login() to use your Claude "
            "subscription, or set ANTHROPIC_API_KEY for the metered API"
            + ("" if have_sdk else " (subscription backend also needs: "
                                   "pip install 'hunch-sdk[agent]')"))

    def reset(self):
        """Clear the conversation for an unrelated task (same Hunch instance and gates)."""
        self.messages = []
        if self._sub is not None:
            self._sub.reset()

    def close(self):
        """Stop the subscription backend's event-loop thread (no-op otherwise)."""
        if self._sub is not None:
            self._sub.close()
            self._sub = None

    def interrupt(self):
        """Stop an in-flight run() while KEEPING the conversation (the next run() continues it).
        Call from another thread (e.g. a Stop button). Subscription backend: interrupts the current
        turn immediately; api backend: cancels before the next turn. No-op if nothing is running."""
        self._abort = True                        # api backend: checked between turns
        if self._sub is not None:
            self._sub.interrupt()                 # subscription backend: immediate

    def _ensure_client(self):
        if self._client is None:
            from .auth import ApiKey
            try:
                import anthropic
            except ImportError as e:
                raise HunchError("the agent loop needs the optional 'anthropic' package — "
                                 "install it with: pip install 'hunch-sdk[agent]'") from e
            if isinstance(self.auth, ApiKey):      # injected key, nothing ambient
                self._client = anthropic.Anthropic(api_key=self.auth.value)
            else:
                self._client = anthropic.Anthropic()   # env key / auth token / ant profile
        return self._client

    def _system(self, system_suffix):
        blocks = [{"type": "text", "text": HUNCH_PLAYBOOK + "\n" + AGENT_ADDENDUM,
                   "cache_control": {"type": "ephemeral"}}]
        if system_suffix:
            blocks.append({"type": "text", "text": system_suffix})
        return blocks

    def _request(self, client, model, max_tokens, system, effort):
        """Provider seam: build kwargs + run one streamed turn, return the final Message."""
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      tools=AGENT_TOOLS, messages=self.messages,
                      thinking={"type": "adaptive"})
        if effort:
            kwargs["output_config"] = {"effort": effort}
        with client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    @staticmethod
    def _accumulate(usage, resp_usage):
        for k in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            usage[k] = usage.get(k, 0) + (getattr(resp_usage, k, 0) or 0)

    def run(self, task, model=None, max_turns=40, on_event=None,
            system_suffix="", effort=None, max_tokens=16000, backend=None):
        """Run the agent loop until Claude finishes the task (end_turn), is blocked, or hits
        max_turns. Returns an AgentResult. on_event(kind, data) receives 'text' | 'tool' |
        'tool_result' | 'done' | 'error' as work streams.

        backend: 'api' | 'subscription' | None (-> the Agent's default, normally 'auto').
        model=None -> claude-opus-4-8 on the api backend, the subscription's own default
        on the subscription backend. max_tokens applies to the api backend only (the
        subscription CLI manages its own output limits)."""
        if self._resolve_backend(backend) == "subscription":
            if self._sub is None:
                from .auth import OAuthToken
                self._sub = _SubscriptionRunner(
                    self._h, auth=self.auth if isinstance(self.auth, OAuthToken) else None,
                    app_id=self._app_id, can_use_tool=self._can_use_tool)
            return self._sub.run(task, model=model, max_turns=max_turns, on_event=on_event,
                                 system_suffix=system_suffix, effort=effort)

        model = model or DEFAULT_MODEL
        client = self._ensure_client()   # friendly HunchError if 'anthropic' isn't installed
        try:
            import anthropic
            _AuthError = anthropic.AuthenticationError
        except Exception:                # injected fake client without anthropic present
            _AuthError = ()
        emit = on_event or (lambda kind, data: None)
        self.messages.append({"role": "user", "content": task})
        usage = {}
        stop_reason, final_text, aborted, turn = "max_turns", "", True, 0
        self._abort = False

        for turn in range(1, max_turns + 1):
            if self._abort:                       # interrupt() between turns (api backend)
                stop_reason, aborted = "interrupted", True
                emit("error", "interrupted")
                break
            _mark_cache(self.messages)
            try:
                resp = self._request(client, model, max_tokens, self._system(system_suffix), effort)
            except _AuthError as e:
                raise HunchError("no valid Anthropic API credentials — set ANTHROPIC_API_KEY, "
                                 "or call hunch.login() to use your Claude subscription "
                                 "(backend='subscription')") from e
            self._accumulate(usage, resp.usage)
            for b in resp.content:
                if getattr(b, "type", None) == "text" and b.text.strip():
                    emit("text", b.text.strip())
            self.messages.append({"role": "assistant", "content": resp.content})  # verbatim replay

            if resp.stop_reason == "tool_use":
                results = []
                for tu in [b for b in resp.content if getattr(b, "type", None) == "tool_use"]:
                    emit("tool", {"name": tu.name, "input": tu.input})
                    r = _run_tool(self._h, tu)
                    emit("tool_result", _preview(r["content"]))
                    results.append(r)
                self.messages.append({"role": "user", "content": results})   # ALL results, ONE message
                continue
            if resp.stop_reason == "pause_turn":   # defensive; no server tools in v1
                continue

            stop_reason = resp.stop_reason
            if stop_reason == "end_turn":
                final_text = "\n".join(b.text for b in resp.content
                                       if getattr(b, "type", None) == "text").strip()
                aborted = False
                emit("done", final_text)
            else:                                   # max_tokens | refusal | other
                emit("error", f"stopped: {stop_reason}")
            break
        else:
            emit("error", f"aborted after max_turns={max_turns}")

        return AgentResult(text=final_text, turns=turn, stop_reason=stop_reason,
                         usage=usage, aborted=aborted)
