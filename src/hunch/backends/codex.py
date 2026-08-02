"""backends/codex.py — the 'codex' backend: OpenAI's Codex agent drives this Mac.

For people who live in Codex rather than Claude Code. Unlike the api/subscription
backends (Anthropic), this hands the whole agent loop to OpenAI's Codex SDK and
exposes Hunch's tools to it as an MCP server — Codex is an MCP *client*, so the
SAME tools the other backends use are the ones Codex drives, via the hunch MCP
server (`hunch serve`).

Two architectural facts follow from Codex spawning its CLI runtime as a subprocess
(it cannot host an in-process MCP server the way claude-agent-sdk can):

  • Tools execute in a SEPARATE `hunch serve` process, not the caller's live Hunch
    instance. Refs stay self-consistent within a session and the tool set/gates are
    identical, but caller-side live state (a simultaneous_mode toggle on THIS object)
    doesn't reach that process.
  • Host-owned permission routing (Agent(can_use_tool=...) / the app's Approve-Deny
    popover) does NOT govern Codex's tool calls. The dangerous verbs are gated by the
    hunch server's own click-to-approve dialogs instead. (Routing Codex's approval
    protocol into the host callback is a future enhancement.)

Auth is a `codex login` (ChatGPT/Codex subscription) session — set it up with
hunch.provider("codex").login() or `codex login`. No API-key path is exposed.

The Codex Python SDK (`openai-codex`, beta) is an optional dependency, imported
lazily. The SDK touch-points are `_open_thread()` and `_run_turn()`; everything else
(config, playbook, result translation) is plain Python and unit-tested with fakes.
"""
import json
import os
import sys

from .base import Backend
from ..errors import HunchError

__all__ = ["CodexBackend"]

# Codex's per-thread instruction channel is additive over its base system prompt, so we
# frame the (coding-oriented) agent as a Mac driver, then hand it the Hunch playbook.
_PREAMBLE = (
    "# Hunch — you are controlling this Mac\n\n"
    "You are NOT doing a software-engineering task. You are driving the user's real "
    "macOS machine on their behalf through the `hunch` MCP tools. Prefer the hunch "
    "tools over your shell/file-editing tools for anything involving apps, files, the "
    "web, or the screen. Follow this playbook exactly.\n\n")


class CodexBackend(Backend):
    """OpenAI Codex as the agent loop, with Hunch's tools attached over MCP."""

    name = "codex"
    provider = "openai"
    extra = "codex"
    default_model = None   # let Codex pick its own default

    def __init__(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
        super().__init__(hunch, auth=auth, app_id=app_id, can_use_tool=can_use_tool)
        self._thread = None            # the live Codex thread (continuation across run())
        self._codex = None             # the Codex client

    # ── availability ────────────────────────────────────────────────────────────
    @classmethod
    def deps_installed(cls):
        try:
            import openai_codex  # noqa: F401
            return True
        except ImportError:
            return False

    @classmethod
    def available(cls, app_id=None):
        # NO ambient credential detection (matches the app's explicit-auth pattern for
        # Claude, which stopped scavenging a detected login because it expired mid-task).
        # A backend is "available" when its SDK is installed; whether a valid Codex login
        # exists is decided by the SDK at run time. Authenticate via hunch.provider("codex")
        # .login() / `codex login`.
        return cls.deps_installed()

    @staticmethod
    def _codex_home():
        return os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))

    # ── tool exposure: the hunch MCP server Codex connects to ────────────────────
    def _mcp_server_command(self):
        """(command, args, env) that launches the hunch MCP server for Codex to consume.
        We run it via the current interpreter (`python -m hunch serve`) so it works
        without relying on the `hunch` script being on the subprocess PATH."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.path.expanduser("~"),
        }
        # deliberately DO NOT set HUNCH_NO_INTERNAL_GATE — we want the server to run its
        # own click-to-approve dialogs, since host permission routing can't reach it here.
        return sys.executable, ["-m", "hunch", "serve"], env

    def _config_overrides(self):
        """Codex `-c key=value` config overrides registering the hunch MCP server, as the
        tuple of TOML-encoded strings CodexConfig(config_overrides=...) expects (nothing on
        disk is edited). json.dumps yields valid TOML for these scalars/arrays."""
        cmd, args, env = self._mcp_server_command()
        ov = [f"mcp_servers.hunch.command={json.dumps(cmd)}",
              f"mcp_servers.hunch.args={json.dumps(args)}"]
        for k, v in env.items():
            ov.append(f"mcp_servers.hunch.env.{k}={json.dumps(v)}")
        return tuple(ov)

    # ── the playbook, delivered as Codex developer instructions ──────────────────
    def _instructions(self):
        from ..playbook import HUNCH_PLAYBOOK
        from ..agent import AGENT_ADDENDUM
        return _PREAMBLE + HUNCH_PLAYBOOK + "\n" + AGENT_ADDENDUM

    def _working_dir(self):
        """A private cwd for the Codex thread, so it never touches the user's project dir."""
        d = os.path.join(self._codex_home(), "hunch-workdir")
        os.makedirs(d, exist_ok=True)
        return d

    # ── result translation: TurnResult.items -> Hunch's on_event contract ────────
    @staticmethod
    def _result_text(result):
        """Pull readable text out of a Codex McpToolCallResult (its .content is a list of
        {type,text} blocks, mirroring MCP tool output); fall back to str()."""
        content = getattr(result, "content", None)
        if isinstance(content, list):
            parts = [c.get("text", "") if isinstance(c, dict) else getattr(c, "text", "")
                     for c in content]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined
        return str(result)

    @classmethod
    def _emit_item(cls, item, emit):
        """Map one ThreadItem (a pydantic RootModel; real variant at .root) onto
        emit(kind, data). Returns the item's agent text if it is a message, else ''."""
        root = getattr(item, "root", item)
        itype = getattr(root, "type", "") or ""
        if itype == "mcpToolCall":
            emit("tool", {"name": getattr(root, "tool", ""),
                          "input": getattr(root, "arguments", None)})
            result = getattr(root, "result", None)
            if result is not None:
                emit("tool_result", cls._result_text(result)[:200])
            return ""
        if itype == "commandExecution":               # Codex ran its own shell (not a hunch tool)
            emit("tool", {"name": "shell", "input": getattr(root, "command", "")})
            return ""
        if itype == "agentMessage":
            text = (getattr(root, "text", "") or "").strip()
            if text:
                emit("text", text)
            return text
        return ""

    @staticmethod
    def _usage_dict(result):
        u = getattr(result, "usage", None)
        if u is None:
            return {}
        dump = getattr(u, "model_dump", None)
        try:
            return dump() if dump else (u if isinstance(u, dict) else {})
        except Exception:
            return {}

    # ── SDK touch-points (isolated so they can be faked in tests) ────────────────
    def _open_thread(self, model, cwd):
        """Create/continue a Codex thread with the hunch MCP server attached and the
        playbook as developer instructions. The one place the SDK's client surface is
        used; override in tests."""
        import openai_codex as oc
        if self._codex is None:
            # Relies on the user's `codex login` session (no API-key path).
            self._codex = oc.Codex(config=oc.CodexConfig(config_overrides=self._config_overrides()))
        if self._thread is None:
            kwargs = dict(cwd=cwd, developer_instructions=self._instructions(),
                          sandbox=oc.Sandbox.workspace_write)
            if model:
                kwargs["model"] = model
            self._thread = self._codex.thread_start(**kwargs)
        return self._thread

    def _run_turn(self, thread, task):
        """Run one turn to completion and return the SDK's TurnResult. Isolated so tests
        fake it; the beta SDK's blocking run(input) returns items + final_response."""
        return thread.run(task)

    # ── run ──────────────────────────────────────────────────────────────────────
    def run(self, task, model=None, max_turns=40, on_event=None,
            system_suffix="", effort=None, max_tokens=None):
        if not self.deps_installed():
            raise HunchError(
                "the 'codex' backend needs the optional 'openai-codex' package (beta) — "
                "install it with: pip install --pre openai-codex  "
                "(or: pip install 'hunch-sdk[codex]')")
        # No credential pre-check: we rely on manual authentication — the SDK's own session
        # from a `hunch.provider("codex").login()` / `codex login`. If nothing is authorized,
        # the SDK raises and we translate it into a login hint below.
        emit = on_event or (lambda kind, data: None)
        model = model or self.default_model
        cwd = self._working_dir()

        try:
            thread = self._open_thread(model, cwd)
            result = self._run_turn(thread, task)
        except Exception as e:  # noqa: BLE001 — surface transport/SDK failures as an error result
            msg = f"codex run failed: {e}"
            if any(w in str(e).lower() for w in ("login", "auth", "unauthor", "credential", "401")):
                msg += " — authenticate first: hunch.provider('codex').login() (or `codex login`)"
            emit("error", msg)
            return _result("", "error", True, {}, 0)

        final = ""
        items = getattr(result, "items", None) or []
        for item in items:
            text = self._emit_item(item, emit)
            if text:
                final = text
        final = final or (getattr(result, "final_response", "") or "")
        err = getattr(result, "error", None)
        aborted = err is not None
        usage = self._usage_dict(result)
        if aborted:
            emit("error", str(err))
        else:
            emit("done", final)
        return _result(final, "error" if aborted else "end_turn", aborted, usage, len(items))

    def reset(self):
        self._thread = None            # drop the conversation; next run() starts a fresh thread

    def close(self):
        self._thread = None
        codex, self._codex = self._codex, None
        if codex is not None:
            closer = getattr(codex, "close", None)
            if closer:
                try:
                    closer()
                except Exception:
                    pass


def _result(text, stop_reason, aborted, usage, turns):
    from ..agent import AgentResult
    return AgentResult(text=text, turns=turns, stop_reason=stop_reason, usage=usage, aborted=aborted)
