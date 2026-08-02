"""backends/base.py — the provider abstraction for the Hunch agent loop.

Every LLM transport the agent can drive this Mac with is a `Backend`: the metered
Anthropic API (`api`), the user's Claude subscription via claude-agent-sdk
(`subscription`), and OpenAI's Codex via the Codex SDK (`codex`). They are peers —
`Agent` is a thin facade that resolves a name to a Backend and delegates to it.

A Backend takes the SAME building blocks every provider shares (defined in
hunch.agent): the AGENT_TOOLS schema, the HUNCH_PLAYBOOK system prompt, the
_dispatch_core tool executor, the AgentResult shape, and the on_event(kind, data)
stream — kind is one of 'text' | 'tool' | 'tool_result' | 'done' | 'error'. A new
provider only has to implement `run()` (its own loop) against those; everything
else (tools, gates, refs, streaming contract) comes for free.

To ADD a provider: subclass Backend, set the class metadata, implement run(), and
register the class in hunch.backends (the REGISTRY). Nothing else in the SDK needs
to learn the new name — Agent, sdk.Hunch validation, and `hunch doctor` all read
the registry.
"""
from abc import ABC, abstractmethod

from ..errors import HunchError

__all__ = ["Backend"]


class Backend(ABC):
    """One LLM transport for the agent loop. Subclasses fill in the class metadata
    and run(); the default interrupt/reset/close are no-ops so a stateless backend
    needn't implement them.

    Class metadata (read by the registry, sdk validation, and `hunch doctor`):
      name          the backend id used in Hunch(agent_backend=...) / run(backend=...)
      provider      the model provider ('anthropic' | 'openai') — the user-facing choice
      extra         the pip extra that installs its optional deps ('hunch-sdk[<extra>]')
      default_model model used when run(model=None), or None to let the transport pick
    """

    name: str = ""
    provider: str = ""
    extra: str = ""
    default_model = None

    def __init__(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
        self._h = hunch
        self._auth = auth              # an injected credential for this provider, or None
        self._app_id = app_id          # namespaces per-app credential storage
        self._permit = can_use_tool    # host-owned permission callback (backends that support it)

    # ── the one thing every provider must implement ─────────────────────────────
    @abstractmethod
    def run(self, task, *, model=None, max_turns=40, on_event=None,
            system_suffix="", effort=None, max_tokens=16000):
        """Drive the task to completion and return an AgentResult. Stream progress by
        calling on_event(kind, data). Backends ignore kwargs that don't apply to them
        (e.g. the subscription/codex transports manage their own output limits, so they
        ignore max_tokens)."""
        raise NotImplementedError

    # ── lifecycle: overridden by stateful backends (default no-ops) ──────────────
    def interrupt(self):
        """Stop an in-flight run() while keeping the conversation (the next run()
        continues it). Called from another thread (e.g. a Stop button)."""

    def reset(self):
        """Drop the conversation so the next run() starts a fresh task."""

    def close(self):
        """Release any resources (event-loop threads, subprocesses, sessions)."""

    # ── availability (deps installed AND a usable credential) ────────────────────
    @classmethod
    def available(cls, app_id=None):
        """True when this backend could run right now — its optional dependency is
        importable AND a credential is resolvable. Used by 'auto' resolution and
        `hunch doctor`. Subclasses override with a real probe."""
        return False

    @classmethod
    def deps_installed(cls):
        """True when the optional dependency package is importable (independent of
        credentials). Subclasses override."""
        return False

    # small shared helper so subclasses raise a consistent missing-dep error
    @classmethod
    def _missing_dep_error(cls, pkg):
        return HunchError(
            f"the {cls.name!r} backend needs the optional {pkg!r} package — "
            f"install it with: pip install 'hunch-sdk[{cls.extra}]'")
