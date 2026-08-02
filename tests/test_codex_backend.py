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


# ── codex result translation (TurnResult.items -> on_event) ───────────────────────
def _item(**kw):
    return types.SimpleNamespace(root=types.SimpleNamespace(**kw))


def test_codex_emit_item():
    events = []
    emit = lambda k, d: events.append((k, d))
    assert CodexBackend._emit_item(_item(type="mcpToolCall", tool="snapshot",
                                         arguments={"app": "Mail"}, result="ok"), emit) == ""
    assert ("tool", {"name": "snapshot", "input": {"app": "Mail"}}) in events
    assert ("tool_result", "ok") in events
    events.clear()
    assert CodexBackend._emit_item(_item(type="agentMessage", text="done"), emit) == "done"
    assert events == [("text", "done")]


class _FakeTurn:
    def __init__(self, items, final_response="", error=None, usage=None):
        self.items = items
        self.final_response = final_response
        self.error = error
        self.usage = usage


def _prep_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(CodexBackend, "deps_installed", classmethod(lambda cls: True))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))


def test_codex_run_streams_and_returns_result(monkeypatch, tmp_path):
    _prep_codex(monkeypatch, tmp_path)
    b = CodexBackend(_FakeHunch())
    monkeypatch.setattr(b, "_open_thread", lambda model, cwd: "THREAD")
    turn = _FakeTurn(items=[_item(type="mcpToolCall", tool="act", arguments={}, result=None),
                            _item(type="agentMessage", text="sent it")],
                     final_response="sent it")
    monkeypatch.setattr(b, "_run_turn", lambda thread, task: turn)
    events = []
    r = b.run("send it", on_event=lambda k, d: events.append(k))
    assert r.text == "sent it" and r.aborted is False and r.stop_reason == "end_turn"
    assert events == ["tool", "text", "done"]


def test_codex_run_error_field(monkeypatch, tmp_path):
    _prep_codex(monkeypatch, tmp_path)
    b = CodexBackend(_FakeHunch())
    monkeypatch.setattr(b, "_open_thread", lambda model, cwd: "THREAD")
    monkeypatch.setattr(b, "_run_turn", lambda t, task: _FakeTurn(items=[], error="rate limited"))
    events = []
    r = b.run("x", on_event=lambda k, d: events.append((k, d)))
    assert r.aborted is True and r.stop_reason == "error"
    assert ("error", "rate limited") in events


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
    import dataclasses
    params = inspect.signature(oc.Codex.thread_start).parameters
    for p in ("cwd", "developer_instructions", "config", "model", "sandbox"):
        assert p in params, f"thread_start lost {p!r}"
    assert hasattr(oc.Sandbox, "workspace_write")
    turn_fields = {f.name for f in dataclasses.fields(oc.TurnResult)}
    for f in ("items", "final_response", "error", "usage"):
        assert f in turn_fields
    # auth methods the provider relies on
    for m in ("login_chatgpt", "login_chatgpt_device_code", "logout", "account"):
        assert hasattr(oc.Codex, m)


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
