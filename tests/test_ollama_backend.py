"""Ollama backend + provider tests — all around the one live transport touch-point
(_chat), faked here so no Ollama server is needed. Mirrors test_codex_backend.py."""
import json
import urllib.error

import pytest

import hunch.agent as agent_mod
from hunch.agent import AGENT_TOOLS
from hunch.backends import NAMES, get
from hunch.backends.ollama import OllamaBackend
from hunch.providers import provider, PROVIDERS, OllamaProvider
from hunch.errors import HunchError


class _FakeHunch:
    def __init__(self):
        self._app_id = None


def _backend(responses, dispatch=None, monkeypatch=None):
    """An OllamaBackend whose _chat pops canned responses and whose tool dispatch is
    recorded (default: every tool returns 'ok')."""
    b = OllamaBackend(_FakeHunch())
    b._sent = []
    seq = list(responses)
    b._chat = lambda body: (b._sent.append(body), seq.pop(0))[1]
    if monkeypatch is not None:
        calls = []
        def fake_dispatch(mac, name, args):
            calls.append((name, args))
            return (dispatch or (lambda n, a: "ok"))(name, args), False
        monkeypatch.setattr(agent_mod, "_dispatch_core", fake_dispatch)
        b._calls = calls
    return b


def _msg(content="", tool_calls=None):
    m = {"message": {"role": "assistant", "content": content}}
    if tool_calls:
        m["message"]["tool_calls"] = tool_calls
    return m


# ── registry ──────────────────────────────────────────────────────────────────
def test_registry_and_metadata():
    assert "ollama" in NAMES
    assert get("ollama") is OllamaBackend
    assert "ollama" in PROVIDERS and isinstance(provider("ollama"), OllamaProvider)
    assert (OllamaBackend.name, OllamaBackend.provider) == ("ollama", "ollama")
    assert OllamaBackend.deps_installed() is True          # stdlib-only


def test_tools_are_agent_tools_in_function_shape():
    tools = OllamaBackend._tools()
    assert [t["function"]["name"] for t in tools] == [t["name"] for t in AGENT_TOOLS]
    assert all(t["type"] == "function" for t in tools)
    assert tools[0]["function"]["parameters"] == AGENT_TOOLS[0]["input_schema"]


# ── the loop ──────────────────────────────────────────────────────────────────
def test_run_native_tool_loop(monkeypatch):
    b = _backend([
        _msg(tool_calls=[{"function": {"name": "list_apps", "arguments": {}}}]),
        _msg("done — Notes is running."),
    ], monkeypatch=monkeypatch)
    events = []
    r = b.run("which apps run?", on_event=lambda k, d: events.append((k, d)))
    assert b._calls == [("list_apps", {})]
    assert r.text == "done — Notes is running." and not r.aborted
    assert r.stop_reason == "end_turn" and r.turns == 2
    assert ("tool", {"name": "list_apps", "input": {}}) in events
    assert events[-1] == ("done", "done — Notes is running.")
    # request shape: playbook system message, think off, explicit num_ctx, tools attached
    body = b._sent[0]
    assert body["think"] is False and body["options"]["num_ctx"] > 0
    assert body["messages"][0]["role"] == "system"
    assert "num_ctx" in body["options"] and body["tools"]
    # tool result went back as a role='tool' message with the tool's name
    tool_msgs = [m for m in b._sent[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs == [{"role": "tool", "tool_name": "list_apps", "content": "ok"}]


def test_run_parses_string_arguments(monkeypatch):
    b = _backend([
        _msg(tool_calls=[{"function": {"name": "snapshot",
                                       "arguments": json.dumps({"app": "Notes"})}}]),
        _msg("ok"),
    ], monkeypatch=monkeypatch)
    b.run("look at Notes")
    assert b._calls == [("snapshot", {"app": "Notes"})]


def test_fallback_parses_prose_tool_call(monkeypatch):
    # The measured qwen3.5 failure mode: a well-formed call in a markdown fence
    # instead of the native channel. It must be recovered AND counted.
    prose = ('I will inspect the app.\n```json\n'
             '{"name": "snapshot", "arguments": {"app": "Notes"}}\n```')
    b = _backend([_msg(prose), _msg("finished")], monkeypatch=monkeypatch)
    r = b.run("look at Notes")
    assert b._calls == [("snapshot", {"app": "Notes"})]
    assert r.usage["fallback_tool_calls"] == 1 and r.text == "finished"


def test_fallback_ignores_non_tool_json(monkeypatch):
    # 3 identical responses: the no-tool-call reply is nudged twice, then accepted.
    resp = _msg('the config is ```json\n{"volume": 11}\n``` — done')
    b = _backend([resp, resp, resp], monkeypatch=monkeypatch)
    r = b.run("read config")
    assert b._calls == [] and r.stop_reason == "end_turn"
    assert r.usage["fallback_tool_calls"] == 0 and r.usage["continue_nudges"] == 2


def test_strict_disables_fallback(monkeypatch):
    monkeypatch.setenv("HUNCH_OLLAMA_STRICT", "1")
    prose = '```json\n{"name": "list_apps", "arguments": {}}\n```'
    resp = _msg(prose)
    b = _backend([resp, resp, resp], monkeypatch=monkeypatch)
    r = b.run("apps?")
    assert b._calls == [] and r.stop_reason == "end_turn"


def test_announce_without_act_is_nudged(monkeypatch):
    # The measured 04/12 failure: a plan narration with no tool call must be pushed
    # back into acting, and the nudge counted.
    b = _backend([
        _msg("I'll read the file and copy the total."),
        _msg(tool_calls=[{"function": {"name": "list_apps", "arguments": {}}}]),
        _msg("done"),
    ], monkeypatch=monkeypatch)
    r = b.run("do it")
    assert b._calls == [("list_apps", {})]
    assert r.usage["continue_nudges"] == 1 and r.text == "done" and not r.aborted
    # once a tool has run, a no-call response ends the episode (no further nudge)
    assert [m["content"] for m in b.messages if m.get("role") == "user"][1].startswith(
        "You have not called any tool")


def test_screenshot_bytes_ride_images_channel(monkeypatch):
    b = _backend([
        _msg(tool_calls=[{"function": {"name": "screenshot", "arguments": {}}}]),
        _msg("saw it"),
    ], dispatch=lambda n, a: b"\x89PNG...", monkeypatch=monkeypatch)
    b.run("what is on screen?")
    tool_msg = [m for m in b._sent[1]["messages"] if m.get("role") == "tool"][0]
    assert tool_msg["images"] and "screenshot" in tool_msg["content"]


def test_max_turns_aborts(monkeypatch):
    call = _msg(tool_calls=[{"function": {"name": "list_apps", "arguments": {}}}])
    b = _backend([call, call], monkeypatch=monkeypatch)
    r = b.run("loop forever", max_turns=2)
    assert r.aborted and r.stop_reason == "max_turns" and r.turns == 2


def test_usage_accumulates_and_reset_clears():
    resp = {**_msg("hi"), "prompt_eval_count": 100, "eval_count": 7}
    b = _backend([resp, resp, resp])       # nudged twice, so three requests total
    r = b.run("hello")
    assert (r.usage["input_tokens"], r.usage["output_tokens"]) == (300, 21)
    assert b.messages          # conversation kept for continuation
    b.reset()
    assert b.messages == []


def test_chat_retries_transient_http_errors(monkeypatch):
    # qwen3.5's tool-template parse flake: Ollama 500s once, then the retry lands.
    import io
    attempts = []
    def flaky_urlopen(req, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(
                "u", 500, "err", {},
                io.BytesIO(b'{"error": "XML syntax error on line 5"}'))
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"message": {"role": "assistant", "content": "hi"}}'
        return _R()
    monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
    out = OllamaBackend(_FakeHunch())._chat({"model": "m"})
    assert out["message"]["content"] == "hi" and len(attempts) == 3


def test_chat_gives_up_after_retries(monkeypatch):
    import io
    def always_500(req, timeout=None):
        raise urllib.error.HTTPError("u", 500, "err", {},
                                     io.BytesIO(b'{"error": "XML syntax error"}'))
    monkeypatch.setattr("urllib.request.urlopen", always_500)
    with pytest.raises(HunchError) as e:
        OllamaBackend(_FakeHunch())._chat({"model": "m"})
    assert "XML syntax error" in str(e.value)


def test_no_server_raises_friendly_hint(monkeypatch):
    b = OllamaBackend(_FakeHunch())
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("refused")))
    with pytest.raises(HunchError) as e:
        b.run("hi")
    assert "ollama serve" in str(e.value)
    assert OllamaBackend.available() is False


# ── provider: reachability-as-auth ────────────────────────────────────────────
def test_provider_status_and_noop_auth(monkeypatch):
    p = OllamaProvider()
    monkeypatch.setattr(OllamaBackend, "available", classmethod(lambda cls, app_id=None: True))
    st = p.status()
    assert (st.provider, st.logged_in, st.method) == ("ollama", True, "local")
    assert p.login().logged_in is True
    assert p.logout() is None
    monkeypatch.setattr(OllamaBackend, "available", classmethod(lambda cls, app_id=None: False))
    assert p.status().logged_in is False
    with pytest.raises(HunchError):
        p.login()


def test_provider_make_backend():
    assert isinstance(OllamaProvider().make_backend(_FakeHunch()), OllamaBackend)
