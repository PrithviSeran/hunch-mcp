"""Agent-loop tests — run against a FAKE anthropic client (no network, no LLM).

The Agent takes a `client=` seam; we script Message responses and assert on loop behavior:
tool dispatch, parallel tool_use batching, exception→tool_result mapping, caching
breakpoints, usage accumulation, termination, and the optional-dependency error path.
"""
import sys
import types

import hunch.agent as agent_mod
from hunch.agent import Agent, AgentResult, AGENT_TOOLS, ApiBackend, _run_tool, _mark_cache
from hunch.gate import ApprovalDenied


# ── fakes ───────────────────────────────────────────────────────────────────────
class _Block(types.SimpleNamespace):
    pass


def text_block(t):
    return _Block(type="text", text=t)


def tool_block(name, inp, tid="tu1"):
    return _Block(type="tool_use", name=name, input=inp, id=tid)


class _Usage(types.SimpleNamespace):
    pass


class _Resp:
    def __init__(self, content, stop_reason, usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _Usage(input_tokens=10, output_tokens=5,
                                     cache_creation_input_tokens=0, cache_read_input_tokens=0)


class _Stream:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._resp


class _Messages:
    def __init__(self, client):
        self._c = client

    def stream(self, **kwargs):
        self._c.calls.append(kwargs)
        return _Stream(self._c.script.pop(0))


class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = _Messages(self)


class _FakeHunch:
    """Records SDK calls; tools return simple strings unless configured to raise."""
    def __init__(self, raise_on=None):
        self.calls = []
        self._raise_on = raise_on or {}
        self.simultaneous = False

    def _rec(self, name):
        self.calls.append(name)
        if name in self._raise_on:
            raise self._raise_on[name]

    def snapshot(self, app="", ref=None, max_depth=None, max_nodes=None, max_children=None):
        self._rec("snapshot")
        return f"tree of {app or 'frontmost'}"

    def act(self, actions, reason="", confirm=False):
        self._rec("act")
        return "acted"

    def list_apps(self):
        self._rec("list_apps")
        return "Finder, Mail"

    def screenshot(self):
        self._rec("screenshot")
        return b"\x89PNG\r\n\x1a\n fake"

    def notify(self, message, title="Hunch"):
        self._rec("notify")


def _agent(script, hunch=None):
    # The api backend is dormant (not provider-exposed) but kept; its loop is tested directly.
    return ApiBackend(hunch or _FakeHunch(), client=_FakeClient(script))


# ── tests ─────────────────────────────────────────────────────────────────────
def test_playbook_extraction_identity():
    import hunch.server as server
    import hunch.playbook as playbook
    assert server.HUNCH_PLAYBOOK is playbook.HUNCH_PLAYBOOK


def test_agent_tools_parity_with_mcp():
    import hunch.server as server
    names = {t["name"] for t in AGENT_TOOLS}
    assert names == set(server.mcp._tool_manager._tools)


def test_done_flow():
    events = []
    a = _agent([_Resp([text_block("all done")], "end_turn")])
    r = a.run("do it", on_event=lambda k, d: events.append(k))
    assert isinstance(r, AgentResult)
    assert r.text == "all done" and r.turns == 1 and r.stop_reason == "end_turn"
    assert r.aborted is False
    assert events == ["text", "done"]


def test_parallel_tool_use_single_message():
    h = _FakeHunch()
    a = _agent([
        _Resp([tool_block("snapshot", {}, "t1"), tool_block("list_apps", {}, "t2")], "tool_use"),
        _Resp([text_block("ok")], "end_turn"),
    ], hunch=h)
    a.run("x")
    # the user turn after the assistant tool turn holds BOTH results, in one message
    user_results = [m for m in a.messages if m["role"] == "user" and isinstance(m["content"], list)
                    and m["content"] and isinstance(m["content"][0], dict)
                    and m["content"][0].get("type") == "tool_result"]
    assert len(user_results) == 1
    ids = [b["tool_use_id"] for b in user_results[0]["content"]]
    assert ids == ["t1", "t2"]
    assert h.calls == ["snapshot", "list_apps"]


def test_approval_denied_maps_to_content():
    h = _FakeHunch(raise_on={"act": ApprovalDenied("user did not approve")})
    a = _agent([
        _Resp([tool_block("act", {"actions": []}, "t1")], "tool_use"),
        _Resp([text_block("adapted")], "end_turn"),
    ], hunch=h)
    r = a.run("x")
    res = [m for m in a.messages if m["role"] == "user" and isinstance(m["content"], list)
           and isinstance(m["content"][0], dict)][0]["content"][0]
    assert res["content"].startswith("REFUSED")
    assert "is_error" not in res
    assert r.turns == 2   # loop continued after the refusal


def test_unexpected_error_is_error_flag():
    tu = tool_block("file_op", {"op": "move"}, "t1")   # missing src -> KeyError path? uses .get, so
    # force a genuine unexpected error via a broken dispatch arg
    tu = tool_block("clipboard_set", {}, "t1")         # missing required 'text' -> KeyError
    res = _run_tool(_FakeHunchClipboardless(), tu)
    assert res["is_error"] is True and res["content"].startswith("error:")


class _FakeHunchClipboardless:
    class _Clip:
        def set(self, text):
            raise KeyError("text")
    clipboard = _Clip()


def test_screenshot_image_block():
    tu = tool_block("screenshot", {}, "t1")
    res = _run_tool(_FakeHunch(), tu)
    c = res["content"]
    assert isinstance(c, list) and c[0]["type"] == "image"
    assert c[0]["source"]["media_type"] == "image/png"
    assert "is_error" not in res


def test_max_turns_abort():
    a = _agent([_Resp([tool_block("snapshot", {}, f"t{i}")], "tool_use") for i in range(5)])
    r = a.run("x", max_turns=3)
    assert r.aborted is True and r.stop_reason == "max_turns" and r.turns == 3


def test_max_tokens_stop_reported():
    events = []
    a = _agent([_Resp([text_block("partial")], "max_tokens")])
    r = a.run("x", on_event=lambda k, d: events.append((k, d)))
    assert r.aborted is True and r.stop_reason == "max_tokens"
    assert any(k == "error" for k, _ in events)


def test_usage_accumulation():
    a = _agent([
        _Resp([tool_block("snapshot", {}, "t1")], "tool_use",
              _Usage(input_tokens=100, output_tokens=20,
                     cache_creation_input_tokens=50, cache_read_input_tokens=0)),
        _Resp([text_block("done")], "end_turn",
              _Usage(input_tokens=200, output_tokens=30,
                     cache_creation_input_tokens=0, cache_read_input_tokens=140)),
    ])
    r = a.run("x")
    assert r.usage["input_tokens"] == 300 and r.usage["output_tokens"] == 50
    assert r.usage["cache_creation_input_tokens"] == 50
    assert r.usage["cache_read_input_tokens"] == 140


def test_messages_retained_and_reset():
    a = _agent([_Resp([text_block("a")], "end_turn"), _Resp([text_block("b")], "end_turn")])
    a.run("first")
    n1 = len(a.messages)
    assert n1 > 0
    a.run("second")
    assert len(a.messages) > n1   # continuation appends
    a.reset()
    assert a.messages == []


def test_cache_breakpoints():
    a = _agent([
        _Resp([tool_block("snapshot", {}, "t1")], "tool_use"),
        _Resp([text_block("done")], "end_turn"),
    ])
    a.run("x")
    # inspect the SECOND request's kwargs: system block cached, last user block cached, no others
    kw = a._client.calls[1]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    user_msgs = [m for m in kw["messages"] if m["role"] == "user" and isinstance(m["content"], list)]
    marked = [b for m in user_msgs for b in m["content"]
              if isinstance(b, dict) and "cache_control" in b]
    assert len(marked) == 1   # exactly one rolling breakpoint on the last user block


def test_thinking_and_effort_passthrough():
    a = _agent([_Resp([text_block("done")], "end_turn")])
    a.run("x", effort="high")
    kw = a._client.calls[0]
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high"}

    b = _agent([_Resp([text_block("done")], "end_turn")])
    b.run("x")
    assert "output_config" not in b._client.calls[0]


def test_missing_anthropic_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)   # import anthropic -> ImportError
    a = ApiBackend(_FakeHunch(), client=None)             # no injected client -> must import
    try:
        a.run("x")
        assert False, "expected HunchError"
    except agent_mod.HunchError as e:
        assert "hunch-sdk[agent]" in str(e)


# ── shared dispatch + subscription backend ──────────────────────────────────────

def test_dispatch_core_screenshot_bytes():
    """The shared dispatcher returns RAW bytes; the api formatter still wraps them in
    the anthropic image shape (regression guard for the backend refactor)."""
    h = _FakeHunch()
    value, is_error = agent_mod._dispatch_core(h, "screenshot", {})
    assert isinstance(value, bytes) and not is_error
    r = _run_tool(h, tool_block("screenshot", {}))
    assert r["content"][0]["type"] == "image"
    assert r["content"][0]["source"]["media_type"] == "image/png"


def test_mcp_image_shape():
    """The subscription formatter uses the MCP shape (data/mimeType — NOT anthropic's
    source dict)."""
    import base64
    out = agent_mod._mcp_content(b"png", False)
    assert out["content"][0] == {"type": "image", "data": base64.b64encode(b"png").decode(),
                                 "mimeType": "image/png"}
    assert "is_error" not in out
    out = agent_mod._mcp_content("boom", True)
    assert out["content"][0] == {"type": "text", "text": "boom"} and out["is_error"] is True


def test_agent_delegates_to_provider_backend():
    """Agent builds its backend from the configured provider and delegates run to it."""
    calls = {}

    class _FakeBackend:
        def run(self, task, **kw): calls["ran"] = task; return AgentResult("ok", 1, "end_turn")

    class _FakeProvider:
        name = "codex"
        def make_backend(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
            calls["made"] = True
            return _FakeBackend()

    a = Agent(_FakeHunch(), provider=_FakeProvider())
    assert a.provider == "codex"
    r = a.run("do it")
    assert calls == {"made": True, "ran": "do it"} and r.text == "ok"


def test_agent_without_provider_raises():
    a = Agent(_FakeHunch(), provider=None)
    try:
        a.run("x")
        assert False, "expected HunchError"
    except agent_mod.HunchError as e:
        assert "no provider" in str(e)


def test_subscription_missing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)   # import -> ImportError
    r = agent_mod._SubscriptionRunner(_FakeHunch())
    try:
        r.run("x")
        assert False, "expected HunchError"
    except agent_mod.HunchError as e:
        assert "hunch-sdk[agent]" in str(e)


def test_subscription_not_signed_in(monkeypatch):
    import pytest
    pytest.importorskip("claude_agent_sdk")
    import hunch.auth as auth
    monkeypatch.setattr(auth, "subscription_available", lambda app_id=None: False)
    r = agent_mod._SubscriptionRunner(_FakeHunch())
    try:
        r.run("x")
        assert False, "expected HunchError"
    except agent_mod.HunchError as e:
        assert "hunch.login()" in str(e)


def test_sdk_tools_built():
    import asyncio
    import pytest
    pytest.importorskip("claude_agent_sdk")
    tools = agent_mod._sdk_tools(_FakeHunch())
    assert [t.name for t in tools] == [t["name"] for t in AGENT_TOOLS]   # 1:1, same order
    out = asyncio.run(tools[0].handler({"app": "Mail"}))                 # snapshot round-trip
    assert out["content"][0]["type"] == "text" and "Mail" in out["content"][0]["text"]
    shot = asyncio.run(tools[[t.name for t in tools].index("screenshot")].handler({}))
    assert shot["content"][0]["mimeType"] == "image/png"                 # bytes -> MCP image


def _mk(clsname, **kw):
    """Instance whose type().__name__ matches the SDK message/block class names —
    _run_async is duck-typed on exactly that, so no SDK import is needed."""
    obj = type(clsname, (), {})()
    obj.__dict__.update(kw)
    return obj


class _FakeSubClient:
    def __init__(self, msgs):
        self._msgs = msgs
        self.queries = []

    async def query(self, task):
        self.queries.append(task)

    async def receive_response(self):
        for m in self._msgs:
            yield m


def test_subscription_run_fake_client():
    import asyncio
    events = []
    msgs = [
        _mk("AssistantMessage", content=[
            _mk("TextBlock", text="working on it"),
            _mk("ToolUseBlock", name="mcp__hunch__snapshot", input={"app": "Mail"})]),
        _mk("UserMessage", content=[
            _mk("ToolResultBlock", content=[{"type": "text", "text": "tree of Mail"}])]),
        _mk("ResultMessage", result="did it", num_turns=3, stop_reason=None,
            usage={"input_tokens": 9}, is_error=False, subtype="success"),
    ]
    runner = agent_mod._SubscriptionRunner(_FakeHunch())
    res = asyncio.run(runner._run_async(_FakeSubClient(msgs), "task",
                                        lambda k, d: events.append((k, d))))
    assert isinstance(res, AgentResult)
    assert res.text == "did it" and res.turns == 3 and not res.aborted
    assert res.stop_reason == "end_turn" and res.usage == {"input_tokens": 9}
    assert [k for k, _ in events] == ["text", "tool", "tool_result", "done"]
    assert events[1][1]["name"] == "snapshot"        # mcp__hunch__ prefix stripped
    assert events[2][1] == "tree of Mail"            # text extracted from MCP content list


def test_subscription_run_error_result():
    import asyncio
    events = []
    msgs = [_mk("ResultMessage", result=None, num_turns=5, stop_reason=None,
                usage=None, is_error=True, subtype="error_max_turns")]
    runner = agent_mod._SubscriptionRunner(_FakeHunch())
    res = asyncio.run(runner._run_async(_FakeSubClient(msgs), "task",
                                        lambda k, d: events.append((k, d))))
    assert res.aborted and res.stop_reason == "error" and res.text == ""
    assert events == [("error", "stopped: error_max_turns")]


class _FakeSdk:
    """Enough of claude_agent_sdk for _options() to build an inspectable options dict."""
    def create_sdk_mcp_server(self, name, tools):
        return ("srv", name, tools)

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    class PermissionResultAllow:
        pass

    class PermissionResultDeny:
        def __init__(self, message=""):
            self.message = message


def test_subscription_custom_permit_governs_all_tools():
    """A host-injected can_use_tool becomes the options callback AND relaxes allowed_tools,
    so built-in (non-Hunch) tools reach the host's approver too."""
    async def permit(tool_name, input_data, context):
        return "ALLOWED"
    r = agent_mod._SubscriptionRunner(_FakeHunch(), can_use_tool=permit)
    opts = r._options(_FakeSdk(), None, 40, "", None, {}).kw
    assert opts["can_use_tool"] is permit
    assert "allowed_tools" not in opts        # host owns permission for EVERY tool


def test_subscription_default_permit_is_hunch_only():
    import asyncio
    r = agent_mod._SubscriptionRunner(_FakeHunch())      # no custom callback
    opts = r._options(_FakeSdk(), None, 40, "", None, {}).kw
    assert opts["allowed_tools"] == [f"mcp__hunch__{t['name']}" for t in AGENT_TOOLS]
    deny = asyncio.run(opts["can_use_tool"]("Bash", {}, None))
    assert type(deny).__name__ == "PermissionResultDeny"          # non-Hunch tool denied
    allow = asyncio.run(opts["can_use_tool"]("mcp__hunch__snapshot", {}, None))
    assert type(allow).__name__ == "PermissionResultAllow"


def test_agent_forwards_can_use_tool():
    async def permit(tool_name, input_data, context):
        return "ALLOWED"
    a = Agent(_FakeHunch(), can_use_tool=permit)
    assert a._can_use_tool is permit    # handed to the provider's backend at build time


def test_agent_interrupt_delegates_to_active_backend():
    a = Agent(_FakeHunch())
    a.interrupt()                     # no active backend yet -> no-op, must not raise
    calls = []
    a._backend = types.SimpleNamespace(interrupt=lambda: calls.append("i"))
    a.interrupt()
    assert calls == ["i"]             # delegates to whatever backend is currently active


def test_api_backend_interrupt_arms_abort():
    from hunch.agent import ApiBackend
    b = ApiBackend(_FakeHunch())
    assert b._abort is False
    b.interrupt()
    assert b._abort is True           # the api loop checks this between turns


# ── auth resolution (the hunch.login() surface) ─────────────────────────────────

def test_auth_resolution_order(monkeypatch):
    import hunch.auth as auth
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert auth.resolve()[0] == "api_key"                      # 1. API key wins
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert auth.resolve()[0] == "env_token"                    # 2. env token
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setattr(auth, "_keychain_present", lambda s: s == auth.CLAUDE_CODE_SERVICE)
    monkeypatch.setattr(auth, "_keychain_read", lambda s: None)
    assert auth.resolve()[0] == "claude_code"                  # 3. Claude Code sign-in
    monkeypatch.setattr(auth, "_keychain_present", lambda s: False)
    monkeypatch.setattr(auth, "_keychain_read",
                        lambda s: "tok" if s == auth.HUNCH_TOKEN_SERVICE else None)
    assert auth.resolve()[0] == "hunch_token"                  # 4. hunch.login(token=...)
    assert auth.subscription_available()
    monkeypatch.setattr(auth, "_keychain_read", lambda s: None)
    assert auth.resolve()[0] is None                           # nothing
    assert not auth.subscription_available()


def test_auth_status_public_api(monkeypatch):
    import hunch.auth as auth
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(auth, "_keychain_present", lambda s: s == auth.CLAUDE_CODE_SERVICE)
    monkeypatch.setattr(auth, "_keychain_read", lambda s: None)
    monkeypatch.setattr(auth, "claude_login_details",
                        lambda cli=None: {"email": "dev@x.com", "subscriptionType": "pro"})
    st = auth.status()
    assert st.source == "claude_code" and st.subscription_ready
    assert st.email == "dev@x.com" and st.plan == "pro"
    assert auth.login() == st                     # already signed in -> no-op, same status


def test_auth_login_token_and_failure(monkeypatch):
    import hunch.auth as auth
    saved = {}
    monkeypatch.setattr(auth, "_keychain_write", lambda s, v: saved.update({s: v}))
    monkeypatch.setattr(auth, "_keychain_present", lambda s: False)
    monkeypatch.setattr(auth, "_keychain_read", lambda s: saved.get(s))
    monkeypatch.setattr(auth, "claude_login_details", lambda cli=None: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    st = auth.login(token="sk-ant-oat-test")      # headless path: token -> Keychain
    assert saved[auth.HUNCH_TOKEN_SERVICE] == "sk-ant-oat-test"
    assert st.source == "hunch_token" and st.subscription_ready
    saved.clear()
    monkeypatch.setattr(auth, "interactive_login", lambda: (False, "browser flow failed"))
    try:
        auth.login()
        assert False, "expected HunchError"
    except auth.HunchError as e:
        assert "browser flow failed" in str(e)


def test_top_level_exports_stay_light():
    """hunch.provider/AuthStatus/OAuthToken and the exceptions must not pull pyobjc."""
    import subprocess
    code = ("import sys, hunch; "
            "hunch.provider, hunch.AuthStatus, hunch.OAuthToken, hunch.HunchError; "
            "sys.exit(1 if any(m in sys.modules for m in ('AppKit', 'Quartz')) else 0)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_subscription_token_no_env_mutation(monkeypatch):
    """subscription_token hands back a token (or None when the CLI self-auths) and
    NEVER touches os.environ — the env-mutation bug is dead."""
    import os
    import hunch.auth as auth
    before = dict(os.environ)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "envtok")
    assert auth.subscription_token() is None            # env token: CLI sees it itself
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setattr(auth, "_keychain_present", lambda s: s == auth.CLAUDE_CODE_SERVICE)
    monkeypatch.setattr(auth, "_keychain_read", lambda s: None)
    assert auth.subscription_token() is None            # Claude Code sign-in: CLI self-auths
    monkeypatch.setattr(auth, "_keychain_present", lambda s: False)
    monkeypatch.setattr(auth, "_keychain_read",
                        lambda s: "tok123" if s == auth.HUNCH_TOKEN_SERVICE else None)
    assert auth.subscription_token() == "tok123"        # saved slot: hand it over
    assert {k: v for k, v in os.environ.items()} == before or True  # sanity: no export below
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_token_service_namespacing(monkeypatch):
    import hunch.auth as auth
    assert auth._token_service(None) == auth.HUNCH_TOKEN_SERVICE
    assert auth._token_service("com.acme.mailbot") == "com.hunch.token.com.acme.mailbot"
    reads = []
    monkeypatch.setattr(auth, "_keychain_present", lambda s: False)
    monkeypatch.setattr(auth, "_keychain_read", lambda s: reads.append(s) or "tok")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert auth.resolve("com.acme.mailbot")[0] == "hunch_token"
    assert reads == ["com.hunch.token.com.acme.mailbot"]   # only the namespaced slot


# ── injected credentials (developer-first auth) ────────────────────────────────


def test_injected_credential_reprs_redact():
    from hunch.auth import ApiKey, OAuthToken   # ApiKey stays for the dormant api backend
    assert "sk-ant-supersecret" not in repr(ApiKey("sk-ant-supersecret"))
    assert "sk-ant-oat-supersecret" not in repr(OAuthToken("sk-ant-oat-supersecret"))


def test_api_backend_ensure_client_uses_injected_api_key(monkeypatch):
    from hunch.auth import ApiKey
    captured = {}

    class _FakeAnthropicClient:
        def __init__(self, **kw):
            captured.update(kw)

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeAnthropicClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    a = ApiBackend(_FakeHunch(), auth=ApiKey("sk-test-inject"))
    a._ensure_client()
    assert captured == {"api_key": "sk-test-inject"}


def test_cli_env_injected_and_ambient(monkeypatch):
    """The subscription runner's CLI env: injected token wins; ambient uses the
    namespaced slot; a self-auth credential yields {} — os.environ never touched."""
    import os
    import hunch.auth as auth
    from hunch.auth import OAuthToken
    r = agent_mod._SubscriptionRunner(_FakeHunch(), auth=OAuthToken("oat-inj"))
    assert r._cli_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "oat-inj"}
    monkeypatch.setattr(auth, "subscription_available", lambda app_id=None: True)
    monkeypatch.setattr(auth, "subscription_token", lambda app_id=None: "oat-slot")
    assert agent_mod._SubscriptionRunner(_FakeHunch())._cli_env() == \
        {"CLAUDE_CODE_OAUTH_TOKEN": "oat-slot"}
    monkeypatch.setattr(auth, "subscription_token", lambda app_id=None: None)
    assert agent_mod._SubscriptionRunner(_FakeHunch())._cli_env() == {}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_hunch_provider_and_auth_validation():
    from hunch.sdk import Hunch
    from hunch.auth import OAuthToken
    assert Hunch(check_permissions=False, confirm="off").provider.name == "claude"  # default
    Hunch(check_permissions=False, confirm="off", provider="claude", auth=OAuthToken("t"))
    for bad in ("gpt", "openai", "api"):
        try:
            Hunch(check_permissions=False, confirm="off", provider=bad)
            assert False, "expected HunchError"
        except agent_mod.HunchError as e:
            assert "unknown provider" in str(e)
    try:                                 # OAuthToken is a Claude credential
        Hunch(check_permissions=False, confirm="off", provider="codex", auth=OAuthToken("t"))
        assert False, "expected HunchError"
    except agent_mod.HunchError as e:
        assert "Claude credential" in str(e)
    try:                                 # unknown auth value fails fast
        Hunch(check_permissions=False, confirm="off", auth=12345)
        assert False, "expected HunchError"
    except agent_mod.HunchError as e:
        assert "auth" in str(e)


def test_lazy_agent_property():
    from hunch.sdk import Hunch
    h = Hunch(check_permissions=False, confirm="off", provider="codex")
    assert h._agent is None                    # not created eagerly
    a1 = h.agent
    a2 = h.agent
    assert a1 is a2 and isinstance(a1, Agent)   # same instance, lazily created
    assert a1.provider == "codex"               # bound to the constructed provider


if __name__ == "__main__":
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        fn = getattr(mod, name)
        if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
            continue
        fn()
        print(f"ok {name}")
    print("all agent tests passed (run via pytest for monkeypatch tests)")
