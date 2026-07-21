"""Agent-loop tests — run against a FAKE anthropic client (no network, no LLM).

The Agent takes a `client=` seam; we script Message responses and assert on loop behavior:
tool dispatch, parallel tool_use batching, exception→tool_result mapping, caching
breakpoints, usage accumulation, termination, and the optional-dependency error path.
"""
import sys
import types

import hunch.agent as agent_mod
from hunch.agent import Agent, AgentResult, AGENT_TOOLS, _run_tool, _mark_cache
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

    def snapshot(self, app="", ref=None, max_depth=None, max_nodes=None):
        self._rec("snapshot")
        return f"tree of {app or 'frontmost'}"

    def act(self, actions, reason=""):
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
    return Agent(hunch or _FakeHunch(), client=_FakeClient(script))


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
    a = Agent(_FakeHunch(), client=None)                  # no injected client -> must import
    try:
        a.run("x")
        assert False, "expected HunchError"
    except agent_mod.HunchError as e:
        assert "hunch-mcp[agent]" in str(e)


def test_lazy_agent_property():
    from hunch.sdk import Hunch
    h = Hunch(check_permissions=False, confirm="off")
    assert h._agent is None                    # not created eagerly
    a1 = h.agent
    a2 = h.agent
    assert a1 is a2 and isinstance(a1, Agent)   # same instance, lazily created


if __name__ == "__main__":
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        fn = getattr(mod, name)
        if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
            continue
        fn()
        print(f"ok {name}")
    print("all agent tests passed (run via pytest for monkeypatch tests)")
