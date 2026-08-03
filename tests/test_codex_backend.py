"""Codex backend + provider-abstraction tests.

Covers the provider layer (hunch.provider / ClaudeProvider / CodexProvider), the codex
agent backend (no API-key path, no credential detection), and the codex auth surface —
all with fakes around the one live SDK touch-point. A final block asserts the shapes we
depend on exist in the installed openai-codex (guards against beta drift).
"""
import sys
import types

import pytest

from hunch.agent import Agent, ApiBackend, SubscriptionBackend, AgentResult
from hunch.backends import NAMES, get
from hunch.backends.codex import CodexBackend
from hunch.providers import provider, PROVIDERS, ClaudeProvider, CodexProvider, AuthStatus
from hunch.errors import HunchError


class _FakeHunch:
    def __init__(self):
        self._app_id = None


# ── provider registry ─────────────────────────────────────────────────────────
def test_provider_registry():
    assert set(PROVIDERS) == {"claude", "codex"}
    assert isinstance(provider("claude"), ClaudeProvider)
    assert isinstance(provider("codex"), CodexProvider)


def test_provider_unknown_raises():
    with pytest.raises(HunchError) as e:
        provider("gpt")
    assert "unknown provider" in str(e.value)


def test_provider_metadata_and_backends():
    assert (ClaudeProvider.name, ClaudeProvider.extra) == ("claude", "subscription")
    assert (CodexProvider.name, CodexProvider.extra) == ("codex", "codex")
    assert isinstance(CodexProvider().make_backend(_FakeHunch()), CodexBackend)
    assert isinstance(ClaudeProvider().make_backend(_FakeHunch()), SubscriptionBackend)


def test_claude_make_backend_passes_oauth_token():
    from hunch.auth import OAuthToken
    be = ClaudeProvider().make_backend(_FakeHunch(), auth=OAuthToken("tok-1"))
    assert be._auth.value == "tok-1"                       # forwarded to the subscription backend


# ── codex backend: no API keys, no credential detection ──────────────────────────
def test_codex_backend_metadata():
    assert (CodexBackend.name, CodexBackend.provider, CodexBackend.extra) == \
        ("codex", "openai", "codex")
    assert CodexBackend.default_model is None


def test_codex_available_is_deps_only(monkeypatch):
    assert not hasattr(CodexBackend, "_credential_present")   # detection removed
    assert not hasattr(CodexBackend, "_api_key")             # API-key path removed
    monkeypatch.setattr(CodexBackend, "deps_installed", classmethod(lambda cls: True))
    assert CodexBackend.available() is True
    monkeypatch.setattr(CodexBackend, "deps_installed", classmethod(lambda cls: False))
    assert CodexBackend.available() is False


def test_codex_run_without_sdk_raises(monkeypatch):
    monkeypatch.setattr(CodexBackend, "deps_installed", classmethod(lambda cls: False))
    with pytest.raises(HunchError) as e:
        CodexBackend(_FakeHunch()).run("do it")
    assert "openai-codex" in str(e.value) and "--pre" in str(e.value)


def test_codex_run_unauthenticated_hints_login(monkeypatch, tmp_path):
    monkeypatch.setattr(CodexBackend, "deps_installed", classmethod(lambda cls: True))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    b = CodexBackend(_FakeHunch())
    monkeypatch.setattr(b, "_open_thread",
                        lambda model, cwd: (_ for _ in ()).throw(RuntimeError("401 please login")))
    events = []
    r = b.run("x", on_event=lambda k, d: events.append((k, d)))
    assert r.aborted is True
    assert any(k == "error" and "login" in d for k, d in events)


def test_codex_config_overrides_register_hunch_server():
    ov = CodexBackend(_FakeHunch())._config_overrides()
    assert isinstance(ov, tuple)
    joined = "\n".join(ov)
    assert "mcp_servers.hunch.command=" in joined
    assert 'mcp_servers.hunch.args=["-m", "hunch", "serve"]' in joined
    assert "mcp_servers.hunch.env.HOME=" in joined and "mcp_servers.hunch.env.PATH=" in joined


def test_codex_instructions_carry_playbook():
    import hunch.playbook as playbook
    text = CodexBackend(_FakeHunch())._instructions()
    assert "controlling this Mac" in text and playbook.HUNCH_PLAYBOOK in text


def test_codex_working_dir_is_private(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    d = CodexBackend(_FakeHunch())._working_dir()
    assert os.path.isdir(d) and str(tmp_path) in d


# ── codex item translation (ThreadItem lifecycle -> on_event) ─────────────────────
def _item(**kw):
    return types.SimpleNamespace(root=types.SimpleNamespace(**kw))


def _note(method, **payload):
    """A fake Codex Notification: `.method` + `.payload` (a namespace of payload fields)."""
    return types.SimpleNamespace(method=method, payload=types.SimpleNamespace(**payload))


def _usage(**kw):
    return types.SimpleNamespace(model_dump=lambda: dict(kw))


def test_codex_emit_call_surfaces_call_at_start():
    # item/started: the tool/command call is shown immediately; no result yet.
    events = []
    emit = lambda k, d: events.append((k, d))
    CodexBackend._emit_call(_item(type="mcpToolCall", tool="snapshot",
                                  arguments={"app": "Mail"}, result=None), emit)
    assert events == [("tool", {"name": "snapshot", "input": {"app": "Mail"}})]
    events.clear()
    CodexBackend._emit_call(_item(type="commandExecution", command="ls -la"), emit)
    assert events == [("tool", {"name": "shell", "input": "ls -la"})]
    events.clear()
    CodexBackend._emit_call(_item(type="agentMessage", text=""), emit)   # messages: nothing at start
    assert events == []


def test_codex_emit_result_surfaces_result_and_text():
    # item/completed: a tool's result, or a message's text (returned so run() tracks final).
    events = []
    emit = lambda k, d: events.append((k, d))
    assert CodexBackend._emit_result(_item(type="mcpToolCall", tool="snapshot",
                                           arguments={}, result="ok"), emit) == ""
    assert events == [("tool_result", "ok")]
    events.clear()
    assert CodexBackend._emit_result(_item(type="agentMessage", text="done"), emit) == "done"
    assert events == [("text", "done")]


def _prep_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(CodexBackend, "deps_installed", classmethod(lambda cls: True))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))


def test_codex_run_streams_live_mid_turn(monkeypatch, tmp_path):
    """The turn's events must reach on_event AS the stream is consumed — not buffered until
    the end. We assert the tool call is already emitted the instant its 'started' notification
    is yielded, before the turn completes."""
    _prep_codex(monkeypatch, tmp_path)
    b = CodexBackend(_FakeHunch())
    monkeypatch.setattr(b, "_open_thread", lambda model, cwd: "THREAD")
    events = []
    emit = lambda k, d: events.append((k, d))

    def fake_stream(thread, task):
        yield _note("item/started", item=_item(type="mcpToolCall", tool="act", arguments={"x": 1}))
        # Proof of LIVE delivery: the call is on the wire before we yield anything further.
        assert ("tool", {"name": "act", "input": {"x": 1}}) in events
        yield _note("item/completed",
                    item=_item(type="mcpToolCall", tool="act", arguments={"x": 1}, result="ok"))
        yield _note("item/started", item=_item(type="agentMessage", text=""))
        yield _note("item/completed", item=_item(type="agentMessage", text="sent it"))
        yield _note("thread/tokenUsage/updated", token_usage=_usage(total=5))
        yield _note("turn/completed", turn=types.SimpleNamespace(error=None))

    monkeypatch.setattr(b, "_start_turn", fake_stream)
    r = b.run("send it", on_event=emit)
    assert [k for k, _ in events] == ["tool", "tool_result", "text", "done"]
    assert r.text == "sent it" and r.aborted is False and r.stop_reason == "end_turn"
    assert r.usage == {"total": 5} and r.turns == 2   # two completed items


def test_codex_consume_falls_back_to_call_at_completion(monkeypatch, tmp_path):
    """If 'started' is ever missing for a tool, its call is still emitted at completion — the
    tool is never shown less than the old buffered path did (Swift renders 'tool', not result)."""
    _prep_codex(monkeypatch, tmp_path)
    b = CodexBackend(_FakeHunch())
    monkeypatch.setattr(b, "_open_thread", lambda model, cwd: "THREAD")
    monkeypatch.setattr(b, "_start_turn", lambda thread, task: iter([
        # note: NO item/started for this tool
        _note("item/completed", item=_item(type="mcpToolCall", id="i9", tool="act",
                                           arguments={"x": 1}, result="ok")),
        _note("turn/completed", turn=types.SimpleNamespace(error=None)),
    ]))
    events = []
    b.run("go", on_event=lambda k, d: events.append((k, d)))
    assert events == [("tool", {"name": "act", "input": {"x": 1}}),
                      ("tool_result", "ok"), ("done", "")]


def test_codex_consume_no_double_call_when_started_present(monkeypatch, tmp_path):
    """When 'started' IS delivered, the call is emitted exactly once (not again at completion)."""
    _prep_codex(monkeypatch, tmp_path)
    b = CodexBackend(_FakeHunch())
    monkeypatch.setattr(b, "_open_thread", lambda model, cwd: "THREAD")
    monkeypatch.setattr(b, "_start_turn", lambda thread, task: iter([
        _note("item/started", item=_item(type="mcpToolCall", id="i9", tool="act", arguments={"x": 1})),
        _note("item/completed", item=_item(type="mcpToolCall", id="i9", tool="act",
                                           arguments={"x": 1}, result="ok")),
        _note("turn/completed", turn=types.SimpleNamespace(error=None)),
    ]))
    events = []
    b.run("go", on_event=lambda k, d: events.append(k))
    assert events == ["tool", "tool_result", "done"]   # exactly one 'tool'


def test_codex_run_error_from_turn_completed(monkeypatch, tmp_path):
    _prep_codex(monkeypatch, tmp_path)
    b = CodexBackend(_FakeHunch())
    monkeypatch.setattr(b, "_open_thread", lambda model, cwd: "THREAD")
    monkeypatch.setattr(b, "_start_turn",
                        lambda thread, task: iter([_note("turn/completed",
                                                         turn=types.SimpleNamespace(error="rate limited"))]))
    events = []
    r = b.run("x", on_event=lambda k, d: events.append((k, d)))
    assert r.aborted is True and r.stop_reason == "error"
    assert ("error", "rate limited") in events


def test_codex_interrupt_cancels_the_handle(monkeypatch, tmp_path):
    _prep_codex(monkeypatch, tmp_path)
    b = CodexBackend(_FakeHunch())
    calls = {"n": 0}
    b._handle = types.SimpleNamespace(interrupt=lambda: calls.__setitem__("n", calls["n"] + 1))
    b.interrupt()
    assert calls["n"] == 1
    b._handle = None
    b.interrupt()   # no live turn -> no-op, no crash
    assert calls["n"] == 1


# ── codex auth surface (via the provider), faking the openai_codex SDK ─────────────
def _fake_openai_codex(account_kind=None, requires_auth=False):
    mod = types.ModuleType("openai_codex")
    calls = {"chatgpt": 0, "device": 0, "logout": 0}

    class _Handle:
        auth_url = "https://auth.example/authorize"
        verification_url = "https://auth.example/device"
        user_code = "WXYZ-7788"

        def wait(self):
            return "done"

    def _acct_root():
        if account_kind is None:
            return None
        obj = type(account_kind, (), {})()          # class NAME is the discriminator
        obj.__dict__.update(email="me@example.com", plan_type="pro")
        return obj

    class _Codex:
        def login_chatgpt(self): calls["chatgpt"] += 1; return _Handle()
        def login_chatgpt_device_code(self): calls["device"] += 1; return _Handle()
        def logout(self): calls["logout"] += 1
        def account(self, refresh_token=False):
            return types.SimpleNamespace(requires_openai_auth=requires_auth,
                                         account=types.SimpleNamespace(root=_acct_root()))

    mod.Codex = _Codex
    return mod, calls


def test_codex_provider_login_and_status(monkeypatch):
    # requires_auth=True mirrors reality: a signed-in ChatGPT account still reports it.
    fake, calls = _fake_openai_codex(account_kind="ChatgptAccount", requires_auth=True)
    monkeypatch.setitem(sys.modules, "openai_codex", fake)
    st = provider("codex").login()
    assert calls["chatgpt"] == 1
    assert isinstance(st, AuthStatus) and st.provider == "codex" and st.logged_in
    assert st.method == "chatgpt" and st.email == "me@example.com" and st.plan == "pro"


def test_codex_provider_device_login_and_logout(monkeypatch):
    fake, calls = _fake_openai_codex()
    monkeypatch.setitem(sys.modules, "openai_codex", fake)
    handle = provider("codex").device_login()
    assert calls["device"] == 1 and handle.user_code == "WXYZ-7788"
    provider("codex").logout()
    assert calls["logout"] == 1


def test_codex_provider_status_logged_out(monkeypatch):
    fake, _ = _fake_openai_codex(account_kind=None)      # no account.root -> logged out
    monkeypatch.setitem(sys.modules, "openai_codex", fake)
    st = provider("codex").status()
    assert st.provider == "codex" and st.logged_in is False


def test_codex_provider_login_without_sdk_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai_codex", None)   # import -> ImportError
    with pytest.raises(HunchError) as e:
        provider("codex").login()
    assert "openai-codex" in str(e.value)


# ── claude provider auth delegates to the (unchanged) claude auth module ──────────
def test_claude_provider_status(monkeypatch):
    import hunch.auth as auth
    monkeypatch.setattr(auth, "status",
                        lambda app_id=None: types.SimpleNamespace(
                            subscription_ready=True, email="dev@x.com", plan="max"))
    st = provider("claude").status()
    assert st.provider == "claude" and st.logged_in and st.method == "subscription"
    assert st.email == "dev@x.com" and st.plan == "max"


# ── Hunch integration: provider drives auth + the agent loop ──────────────────────
def test_hunch_provider_selection():
    from hunch.sdk import Hunch
    h = Hunch(check_permissions=False, confirm="off", provider="codex")
    assert h.provider.name == "codex" and h.agent.provider == "codex"
    assert isinstance(h.agent._backend_(), CodexBackend)   # agent uses the provider's backend


def test_hunch_login_dispatches_to_provider(monkeypatch):
    from hunch.sdk import Hunch
    fake, calls = _fake_openai_codex(account_kind="ChatgptAccount", requires_auth=True)
    monkeypatch.setitem(sys.modules, "openai_codex", fake)
    h = Hunch(check_permissions=False, confirm="off", provider="codex")
    st = h.login()                                          # no provider prefix
    assert calls["chatgpt"] == 1 and st.provider == "codex" and st.logged_in


# ── real-SDK shape guards (skip if openai-codex isn't installed) ───────────────────
def test_sdk_shape_matches_backend_assumptions():
    oc = pytest.importorskip("openai_codex")
    import inspect
    params = inspect.signature(oc.Codex.thread_start).parameters
    for p in ("cwd", "developer_instructions", "config", "model", "sandbox"):
        assert p in params, f"thread_start lost {p!r}"
    assert hasattr(oc.Sandbox, "workspace_write")
    # streaming turn surface we now drive (thread.turn -> TurnHandle.stream/.interrupt)
    assert hasattr(oc.Thread, "turn")
    for m in ("stream", "interrupt"):
        assert hasattr(oc.TurnHandle, m), f"TurnHandle lost {m!r}"
    # auth methods the provider relies on
    for m in ("login_chatgpt", "login_chatgpt_device_code", "logout", "account"):
        assert hasattr(oc.Codex, m)


def test_sdk_streaming_notification_payloads_match():
    """The notification payload fields _consume reads: item on started/completed,
    token_usage on the usage notification, turn (with .error) on turn/completed."""
    pytest.importorskip("openai_codex")
    from openai_codex import models as m
    assert "item" in m.ItemStartedNotification.model_fields
    assert "item" in m.ItemCompletedNotification.model_fields
    assert "token_usage" in m.ThreadTokenUsageUpdatedNotification.model_fields
    assert "turn" in m.TurnCompletedNotification.model_fields


def test_sdk_accepts_our_config_overrides():
    oc = pytest.importorskip("openai_codex")
    oc.CodexConfig(config_overrides=CodexBackend(_FakeHunch())._config_overrides())


def test_sdk_item_variant_fields_match():
    pytest.importorskip("openai_codex")
    import typing
    from openai_codex.generated import v2_all as v
    mcp = v.McpToolCallThreadItem.model_fields
    assert "tool" in mcp and "arguments" in mcp and "result" in mcp
    assert typing.get_args(mcp["type"].annotation) == ("mcpToolCall",)
    assert typing.get_args(v.AgentMessageThreadItem.model_fields["type"].annotation) == ("agentMessage",)


def test_consume_routes_real_sdk_notifications():
    """End-to-end over REAL openai-codex objects (not fakes): build the actual Notification /
    ThreadItem / Turn pydantic models a live turn emits and run them through the real _consume,
    proving every attribute read (.method/.payload/.item.root.type/.tool/.result/.turn.error)
    matches the installed SDK. Complements the SimpleNamespace unit tests, which only encode
    our assumptions about those shapes."""
    pytest.importorskip("openai_codex")
    from openai_codex.models import (
        Notification, ItemStartedNotification, ItemCompletedNotification,
        TurnCompletedNotification, ThreadTokenUsageUpdatedNotification)
    from openai_codex.generated import v2_all as v

    def mcp(status, result=None):
        return v.McpToolCallThreadItem(id="i1", type="mcpToolCall", server="hunch", tool="act",
                                       arguments={"x": 1}, status=status, result=result)

    def bd(n):
        return v.TokenUsageBreakdown(cached_input_tokens=0, input_tokens=n, output_tokens=n,
                                     reasoning_output_tokens=0, total_tokens=2 * n)

    ok = v.McpToolCallResult(content=[{"type": "text", "text": "ok"}])
    stream = [
        Notification(method="item/started", payload=ItemStartedNotification(
            item=mcp(v.McpToolCallStatus.in_progress), startedAtMs=1, threadId="th", turnId="t1")),
        Notification(method="item/completed", payload=ItemCompletedNotification(
            item=mcp(v.McpToolCallStatus.completed, result=ok), completedAtMs=2,
            threadId="th", turnId="t1")),
        Notification(method="item/completed", payload=ItemCompletedNotification(
            item=v.AgentMessageThreadItem(id="m1", type="agentMessage", text="sent it"),
            completedAtMs=3, threadId="th", turnId="t1")),
        Notification(method="thread/tokenUsage/updated", payload=ThreadTokenUsageUpdatedNotification(
            threadId="th", turnId="t1", tokenUsage=v.ThreadTokenUsage(last=bd(5), total=bd(21)))),
        Notification(method="turn/completed", payload=TurnCompletedNotification(
            threadId="th", turn=v.Turn(id="t1", items=[], status=v.TurnStatus.completed))),
    ]
    b = CodexBackend(_FakeHunch())
    events = []
    final, error, usage, items = b._consume(iter(stream), lambda k, d: events.append((k, d)))
    assert events == [
        ("tool", {"name": "act", "input": {"x": 1}}),   # surfaced at item/started
        ("tool_result", "ok"),                            # surfaced at item/completed
        ("text", "sent it"),
    ]
    assert final == "sent it" and error is None and items == 2
    assert usage["total"]["total_tokens"] == 42

    # a failed turn surfaces its TurnError
    _, err2, _, _ = b._consume(iter([
        Notification(method="turn/completed", payload=TurnCompletedNotification(
            threadId="th", turn=v.Turn(id="t1", items=[], status=v.TurnStatus.failed,
                                       error=v.TurnError(message="boom"))))]),
        lambda k, d: None)
    assert getattr(err2, "message", None) == "boom"
