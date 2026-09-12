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
from .backends.base import Backend   # dependency-free ABC; no import cycle (see backends/__init__)

__all__ = ["Agent", "AgentResult", "ApiBackend", "SubscriptionBackend"]

DEFAULT_MODEL = "claude-opus-4-8"

# ── tool schemas ──────────────────────────────────────────────────────────────
# The full-input action item (native AX layer). Shared by the `act` tool.
from .tool_registry import catalog, catalog_for, _ACTION_ITEM, _WEB_ACTION_ITEM, _obj
AGENT_TOOLS = catalog()

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
                              confirm=a.get("confirm", False), detailed=a.get("detailed", False),
                              postcondition=a.get("postcondition")),
    "screenshot": lambda m, a: m.screenshot(),
    "list_apps": lambda m, a: m.list_apps(),
    "launch_app": lambda m, a: m.launch_app(a["name"], force_accessibility=a.get("force_accessibility", False),
                                            reason=a.get("reason", "")),
    "simultaneous_mode": lambda m, a: _set_simultaneous(m, a.get("on", True)),
    "quit_app": lambda m, a: m.quit_app(a["name"]),
    "focus_app": lambda m, a: m.focus_app(a["name"], reason=a.get("reason", "")),
    "web_open": lambda m, a: m.web.open(url=a.get("url", ""), app=a.get("app", "Google Chrome"),
                                        isolated=a.get("isolated", False),
                                        new_window=a.get("new_window", False)),
    "web_login": lambda m, a: m.web.login(url=a.get("url", ""), app=a.get("app", "Google Chrome")),
    "web_snapshot": lambda m, a: m.web.snapshot(),
    "web_screenshot": lambda m, a: m.web.screenshot(),
    "web_act": lambda m, a: m.web.act(a.get("actions", []), detailed=a.get("detailed", False),
                                     postcondition=a.get("postcondition")),
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


_DISPATCH.update({
    "app_target": lambda m, a: m.targets(**a),
    "app_capabilities": lambda m, a: m.capabilities(**a),
    "app_recover": lambda m, a: m.recover(**a),
})

def _set_simultaneous(mac, on):
    mac.simultaneous = bool(on)
    return ("simultaneous mode ON — Hunch will not touch your foreground, cursor, or keyboard"
            if on else "simultaneous mode OFF — Hunch may bring apps forward and use the cursor/keyboard")


def _notify(mac, message):
    delivered = mac.notify(message, f"{getattr(mac, 'app_name', 'Hunch')} needs you")
    if delivered is False:
        return ("user-attention notifications are disabled; ask the user directly in your "
                f"response: {message}")
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
    # Once Safari has been opened through the extension, native AX/vision is the
    # wrong surface: it sees browser chrome or the physical foreground, not the
    # bound page.  Models occasionally "recover" from a perfectly good web_open
    # by targeting Safari with snapshot/act/screenshot; refuse that silent layer
    # downgrade so they stay on web_snapshot/web_act/web_screenshot.
    web = getattr(mac, "web", None)
    web_computer = getattr(web, "_computer", None)
    if getattr(web_computer, "backend", None) == "safari":
        requested_app = str((args or {}).get("app", "")).strip().lower()
        requested_name = str((args or {}).get("name", "")).strip().lower()
        safari_names = {"safari", "apple safari"}
        wrong_surface = (
            name in {"act", "screenshot"}
            or (name in {"snapshot", "find"} and (not requested_app or requested_app in safari_names))
            or (name in {"app_target", "app_capabilities"} and requested_app in safari_names)
            or (name in {"focus_app", "launch_app"} and requested_name in safari_names)
        )
        if wrong_surface:
            return (
                "WRONG SURFACE: Safari is connected through the Hunch extension. "
                "Use web_snapshot, web_act, or web_screenshot on the bound page; do not "
                "fall back to native snapshot, act, screenshot, app targeting, or focus.",
                False,
            )
    redact = getattr(mac, "_redact", lambda value: value)
    try:
        from .tool_registry import validate_arguments
        value = fn(mac, validate_arguments(name, args or {}))
        return mac._redact(value) if hasattr(mac, "_redact") else value, False
    except ApprovalDenied as e:
        return redact(f"REFUSED: {e} — do not retry the identical action; adapt or ask the user"), False
    except StaleRef:
        return "ref is stale — re-snapshot the app and use fresh refs", False
    except (WebNotOpen, AccessibilityNotGranted, HunchError) as e:
        return redact(str(e)), False
    except Exception as e:  # noqa: BLE001 — genuinely unexpected: flag it
        return redact(f"error: {e}"), True


def _run_tool(mac, tool_use):
    """API-backend formatter: dispatch one tool_use block -> an anthropic tool_result dict
    (bytes become an anthropic-shaped image block)."""
    value, is_error = _dispatch_core(mac, tool_use.name, tool_use.input or {})
    content = _img(value) if isinstance(value, bytes) else (json.dumps(value) if isinstance(value, dict) else value)
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
        content = [{"type": "text", "text": json.dumps(value) if isinstance(value, dict) else str(value)}]
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
            for t in catalog_for(mac)]


class SubscriptionBackend(Backend):
    """The subscription backend: claude-agent-sdk on the user's Claude sign-in
    (hunch.login() / Claude Code / CLAUDE_CODE_OAUTH_TOKEN — see hunch.auth).

    Sync facade over the SDK's asyncio client: a daemon event-loop thread plus
    run_coroutine_threadsafe, so Agent.run() stays synchronous. The connected client
    is kept across run() calls (continuation) while the options are unchanged."""

    name = "subscription"
    provider = "anthropic"
    extra = "subscription"
    default_model = None   # let the subscription CLI pick its own default model

    def __init__(self, hunch, auth=None, app_id=None, can_use_tool=None):
        self._h = hunch
        self._auth = auth              # OAuthToken (injected) or None (ambient)
        self._app_id = app_id
        self._permit = can_use_tool    # host-owned permission callback (see Agent.__init__)
        self._loop = None
        self._thread = None
        self._client = None
        self._opts_key = None

    @classmethod
    def deps_installed(cls):
        try:
            import claude_agent_sdk  # noqa: F401
            return True
        except ImportError:
            return False

    @classmethod
    def available(cls, app_id=None):
        from . import auth as auth_mod
        return cls.deps_installed() and auth_mod.subscription_available(app_id)

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
        if hasattr(self._h, "session_summary"):
            system += "\n" + self._h.session_summary()
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
            opts["allowed_tools"] = [f"mcp__hunch__{t['name']}" for t in catalog_for(self._h)]
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

    def run(self, task, model=None, max_turns=40, on_event=None, system_suffix="",
            effort=None, max_tokens=None):   # max_tokens ignored: the CLI manages output limits
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


# Back-compat + registry alias: the subscription backend used to be named _SubscriptionRunner.
# Tests and older callers import hunch.agent._SubscriptionRunner; keep it pointing at the class.
_SubscriptionRunner = SubscriptionBackend


class ApiBackend(Backend):
    """The 'api' backend: our own tool loop on the `anthropic` SDK, metered on an
    ANTHROPIC_API_KEY (env, or an injected hunch.ApiKey). Tools run IN-PROCESS against
    the caller's live Hunch instance via _dispatch_core, so refs, gates, and simultaneous
    mode are exactly the caller's. Conversation state (messages) lives here so run() can
    continue a task across calls; reset() clears it."""

    name = "api"
    provider = "anthropic"
    extra = "api"
    default_model = DEFAULT_MODEL

    def __init__(self, hunch, *, client=None, auth=None, app_id=None):
        self._h = hunch
        self._client = client          # test seam; None -> lazily anthropic.Anthropic()
        self._auth = auth              # ApiKey (injected) or None (ambient)
        self._app_id = app_id
        self.messages = []
        self._abort = False            # between-turns cancel flag (see interrupt())

    @classmethod
    def deps_installed(cls):
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    @classmethod
    def available(cls, app_id=None):
        return cls.deps_installed() and bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _ensure_client(self):
        if self._client is None:
            from .auth import ApiKey
            try:
                import anthropic
            except ImportError as e:
                raise HunchError("the agent loop needs the optional 'anthropic' package — "
                                 "install it with: pip install 'hunch-sdk[agent]'") from e
            if isinstance(self._auth, ApiKey):     # injected key, nothing ambient
                self._client = anthropic.Anthropic(api_key=self._auth.value)
            else:
                self._client = anthropic.Anthropic()   # env key / auth token / ant profile
        return self._client

    def _system(self, system_suffix):
        blocks = [{"type": "text", "text": HUNCH_PLAYBOOK + "\n" + AGENT_ADDENDUM,
                   "cache_control": {"type": "ephemeral"}}]
        if hasattr(self._h, "session_summary"):
            blocks.append({"type": "text", "text": self._h.session_summary()})
        if system_suffix:
            blocks.append({"type": "text", "text": system_suffix})
        return blocks

    def _request(self, client, model, max_tokens, system, effort):
        """Provider seam: build kwargs + run one streamed turn, return the final Message."""
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      tools=catalog_for(self._h), messages=self.messages,
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

    def interrupt(self):
        self._abort = True             # checked between turns

    def reset(self):
        self.messages = []

    def run(self, task, model=None, max_turns=40, on_event=None,
            system_suffix="", effort=None, max_tokens=16000):
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
            if self._abort:                       # interrupt() between turns
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


class Agent:
    """The agent loop, created lazily as `Hunch.agent`. Runs the loop on the Hunch instance's
    configured PROVIDER (hunch.provider.Provider): it builds that provider's Backend on first
    use and delegates run/interrupt/reset/close to it, holding it across run() calls so a task
    continues; reset() starts fresh. The provider — not the Agent — decides the transport, so
    there is no backend selection here."""

    def __init__(self, hunch, provider=None, auth=None, can_use_tool=None):
        self._h = hunch
        self._provider = provider      # a hunch.provider.Provider (claude / codex)
        self.auth = auth               # provider-appropriate injected credential, or None
        # Optional host-owned permission callback handed to backends that support it (the
        # Hunch app routes each tool through its own Approve/Deny UI via this).
        self._can_use_tool = can_use_tool
        self._backend = None           # the provider's Backend, built on first run

    @property
    def _app_id(self):
        return getattr(self._h, "_app_id", None)   # set by Hunch when namespaced

    @property
    def provider(self):
        """The configured provider's name ('claude' | 'codex'), or None."""
        return getattr(self._provider, "name", None)

    @property
    def messages(self):
        """The backend's conversation, when it keeps one here (empty for delegated backends
        that hold their transcript inside their own SDK session)."""
        return getattr(self._backend, "messages", [])

    def _backend_(self):
        if self._backend is None:
            if self._provider is None:
                raise HunchError("no provider set for the agent loop — construct "
                                 "Hunch(provider='claude') or Hunch(provider='codex')")
            self._backend = self._provider.make_backend(
                self._h, auth=self.auth, app_id=self._app_id, can_use_tool=self._can_use_tool)
        return self._backend

    def reset(self):
        """Clear the conversation for an unrelated task (same Hunch instance and gates)."""
        if self._backend is not None:
            self._backend.reset()

    def close(self):
        """Release the backend's resources (event-loop threads, sessions)."""
        if self._backend is not None:
            self._backend.close()
            self._backend = None

    def interrupt(self):
        """Stop an in-flight run() while KEEPING the conversation (the next run() continues it).
        Call from another thread (e.g. a Stop button). No-op if nothing is running."""
        if self._backend is not None:
            self._backend.interrupt()

    def run(self, task, model=None, max_turns=40, on_event=None,
            system_suffix="", effort=None, max_tokens=16000):
        """Run the agent loop until the model finishes the task (end_turn), is blocked, or hits
        max_turns. Returns an AgentResult. on_event(kind, data) receives 'text' | 'tool' |
        'tool_result' | 'done' | 'error' as work streams. model=None uses the provider's own
        default model."""
        return self._backend_().run(task, model=model, max_turns=max_turns, on_event=on_event,
                                    system_suffix=system_suffix, effort=effort, max_tokens=max_tokens)
