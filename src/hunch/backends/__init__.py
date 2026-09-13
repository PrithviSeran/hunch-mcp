"""backends — the provider registry for the agent loop.

The single source of truth for "what backends exist". Agent, sdk.Hunch's
constructor validation, and `hunch doctor` all read this, so adding a provider is
one entry here (plus its Backend subclass) — not edits scattered across the SDK.

Imports are lazy (inside the functions) so that `import hunch.backends` — and
`from .backends.base import Backend`, which agent.py does at import time — never
pulls in the agent module or the optional LLM SDKs. `hunch.backends.base` stays
dependency-free; the concrete backends load only when actually resolved.
"""
from .base import Backend

__all__ = ["Backend", "NAMES", "get", "available", "meta", "provider_names"]

# Ordered: 'auto' resolution prefers earlier entries when several are usable.
NAMES = ("api", "subscription", "codex", "ollama")


def get(name):
    """The Backend subclass for a registered name, or raise HunchError for an
    unknown name (listing the valid ones)."""
    if name == "api":
        from ..agent import ApiBackend
        return ApiBackend
    if name == "subscription":
        from ..agent import SubscriptionBackend
        return SubscriptionBackend
    if name == "codex":
        from .codex import CodexBackend
        return CodexBackend
    if name == "ollama":
        from .ollama import OllamaBackend
        return OllamaBackend
    from ..errors import HunchError
    raise HunchError(f"unknown backend {name!r} — use "
                     + ", ".join(repr(n) for n in NAMES) + ", or 'auto'")


def available(name, app_id=None):
    """True when the named backend could run right now (deps installed AND a
    credential resolvable)."""
    return get(name).available(app_id)


def meta(name):
    """(provider, extra, default_model) for a registered backend — cheap, no
    credential probing."""
    cls = get(name)
    return cls.provider, cls.extra, cls.default_model


def provider_names():
    """Map of backend name -> user-facing provider ('anthropic' | 'openai')."""
    return {n: get(n).provider for n in NAMES}
