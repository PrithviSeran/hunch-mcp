"""provider.py — the LLM provider abstraction for Hunch.

A Provider bundles one vendor's AUTH (login / logout / status) with its AGENT backend,
so the whole surface is provider-agnostic once chosen at construction:

    mac = hunch.Hunch(provider="codex")     # or "claude" (the default)
    mac.login()                             # -> the configured provider's sign-in
    mac.status()                            # -> AuthStatus
    mac.logout()
    mac.agent.run("reply to Sarah's email") # -> runs on the configured provider

Standalone, without a Mac instance (e.g. a setup script):

    hunch.provider("codex").login()
    hunch.provider("claude").status()

Only SUBSCRIPTION / login transports are exposed — metered API keys are intentionally
left out for now (they add confusion and nobody drives Hunch on their own key). Each
provider therefore maps to exactly one login-based agent backend. The exception is
'ollama' (a local model): it has no account at all, so its auth surface is a
server-reachability check rather than a sign-in.

To add a vendor: subclass Provider, implement login/logout/status/make_backend, and
register it in PROVIDERS. Nothing else in the SDK needs to learn the new name.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .errors import HunchError

__all__ = ["Provider", "AuthStatus", "PROVIDERS", "provider", "names"]


@dataclass
class AuthStatus:
    """A provider's sign-in state, uniform across vendors.
    method is the transport that authenticates ('subscription' | 'chatgpt' | ...)."""
    provider: str
    logged_in: bool
    method: str | None = None
    email: str | None = None
    plan: str | None = None


class Provider(ABC):
    """One LLM vendor: its sign-in flow plus the agent backend it drives Hunch with."""

    name: str = ""
    extra: str = ""        # the pip extra that installs this provider's agent backend

    # ── auth ────────────────────────────────────────────────────────────────────
    @abstractmethod
    def login(self, **kwargs):
        """Authorize this provider (interactive by default). Returns an AuthStatus."""

    @abstractmethod
    def logout(self, **kwargs):
        """Sign this provider out."""

    @abstractmethod
    def status(self, **kwargs) -> "AuthStatus":
        """Who, if anyone, is signed in for this provider (explicit check, no scavenging)."""

    def device_login(self, **kwargs):
        """Headless/remote sign-in, for providers that support it."""
        raise HunchError(f"the {self.name!r} provider has no device-login flow")

    # ── agent backend ────────────────────────────────────────────────────────────
    @abstractmethod
    def make_backend(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
        """Build the Backend that runs the agent loop for this provider."""

    @classmethod
    def deps_installed(cls):
        """True when this provider's agent backend dependency is importable."""
        return False


class ClaudeProvider(Provider):
    """Anthropic Claude on the user's subscription (claude-agent-sdk + hunch.login sign-in)."""

    name = "claude"
    extra = "subscription"

    def login(self, token=None, app_id=None):
        from . import auth
        return _from_claude(auth.login(token=token, app_id=app_id))

    def logout(self, app_id=None):
        from . import auth
        return auth.logout(app_id=app_id)

    def status(self, app_id=None):
        from . import auth
        return _from_claude(auth.status(app_id=app_id))

    def make_backend(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
        from .agent import SubscriptionBackend
        from .auth import OAuthToken
        token = auth if isinstance(auth, OAuthToken) else None
        return SubscriptionBackend(hunch, auth=token, app_id=app_id, can_use_tool=can_use_tool)

    @classmethod
    def deps_installed(cls):
        from .agent import SubscriptionBackend
        return SubscriptionBackend.deps_installed()


class CodexProvider(Provider):
    """OpenAI Codex on a `codex login` (ChatGPT) session (the openai-codex SDK)."""

    name = "codex"
    extra = "codex"

    def login(self, **kwargs):
        from . import auth
        return _from_codex(auth.codex_login())          # ChatGPT browser sign-in

    def device_login(self, **kwargs):
        from . import auth
        return auth.codex_device_login()                 # returns the SDK's device handle

    def logout(self, **kwargs):
        from . import auth
        return auth.codex_logout()

    def status(self, **kwargs):
        from . import auth
        return _from_codex(auth.codex_status())

    def make_backend(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
        from .backends.codex import CodexBackend
        return CodexBackend(hunch, app_id=app_id, can_use_tool=can_use_tool)

    @classmethod
    def deps_installed(cls):
        from .backends.codex import CodexBackend
        return CodexBackend.deps_installed()


class OllamaProvider(Provider):
    """A local model served by Ollama — no account, so the auth surface degrades to a
    server-reachability check: login() verifies the daemon answers (raising with the
    fix when it doesn't), status() reports reachable-as-signed-in, logout() is a no-op."""

    name = "ollama"
    extra = ""                      # stdlib transport; nothing to install

    def _reachable(self):
        from .backends.ollama import OllamaBackend
        return OllamaBackend.available()

    def login(self, **kwargs):
        from .backends.ollama import OllamaBackend
        if not self._reachable():
            raise HunchError(f"no Ollama server at {OllamaBackend._host()} — start it "
                             "with `ollama serve` (or install: brew install ollama)")
        return self.status()

    def logout(self, **kwargs):
        return None                 # nothing to sign out of

    def status(self, **kwargs):
        from .backends.ollama import OllamaBackend
        up = self._reachable()
        return AuthStatus("ollama", up, "local" if up else None,
                          plan=OllamaBackend.default_model if up else None)

    def make_backend(self, hunch, *, auth=None, app_id=None, can_use_tool=None):
        from .backends.ollama import OllamaBackend
        return OllamaBackend(hunch, app_id=app_id, can_use_tool=can_use_tool)

    @classmethod
    def deps_installed(cls):
        return True


PROVIDERS = {"claude": ClaudeProvider, "codex": CodexProvider, "ollama": OllamaProvider}


def names():
    return tuple(PROVIDERS)


def provider(name):
    """The Provider for a name ('claude' | 'codex'), usable standalone:
    hunch.provider('codex').login()."""
    cls = PROVIDERS.get(name)
    if cls is None:
        raise HunchError(f"unknown provider {name!r} — use "
                         + ", ".join(repr(n) for n in PROVIDERS))
    return cls()


# ── adapters: vendor-specific status -> the uniform AuthStatus ──────────────────
def _from_claude(st):
    return AuthStatus("claude", bool(getattr(st, "subscription_ready", False)),
                      "subscription" if getattr(st, "subscription_ready", False) else None,
                      getattr(st, "email", None), getattr(st, "plan", None))


def _from_codex(st):
    return AuthStatus("codex", bool(getattr(st, "logged_in", False)),
                      getattr(st, "method", None), getattr(st, "email", None),
                      getattr(st, "plan", None))
