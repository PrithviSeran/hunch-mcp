"""Hunch — drive your Mac focus-free over MCP.

This module is intentionally dependency-free: the CLI (config, creds, connect,
doctor) must work even when pyobjc or the MCP SDK are missing, so nothing here
may import them. The server loads lazily via `hunch serve`.
"""

__version__ = "0.6.0"

# Public SDK surface, loaded lazily (PEP 562) so bare `import hunch` stays dependency-free.
# Names mapped to .sdk/.agent/.local_mac pull in pyobjc; .errors/.auth/.providers stay
# stdlib-only, so hunch.provider(...)/AuthStatus and the exceptions work without the heavy deps.
_LAZY = {
    "Hunch": ".sdk",
    "Agent": ".agent",
    "AgentResult": ".agent",
    "HunchError": ".errors",
    "ApprovalDenied": ".errors",
    "AccessibilityNotGranted": ".errors",
    "WebNotOpen": ".errors",
    "StaleRef": ".local_mac",
    "provider": ".providers",   # hunch.provider("claude"|"codex") — auth + agent loop, standalone
    "Provider": ".providers",
    "AuthStatus": ".providers",  # the uniform sign-in status returned by provider.status()
    "OAuthToken": ".auth",      # the Claude subscription token, for Hunch(provider="claude", auth=...)
    "ConsentRequest": ".errors",   # what a custom confirm callback receives
}


def __getattr__(name):
    from importlib import import_module
    if name == "auth":                 # the whole submodule: hunch.auth.status() etc.
        return import_module(".auth", __name__)
    if name in _LAZY:
        return getattr(import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module 'hunch' has no attribute {name!r}")


def __dir__():
    return sorted([*globals(), *_LAZY, "auth"])
