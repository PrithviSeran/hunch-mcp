"""Hunch — drive your Mac focus-free over MCP.

This module is intentionally dependency-free: the CLI (config, creds, connect,
doctor) must work even when pyobjc or the MCP SDK are missing, so nothing here
may import them. The server loads lazily via `hunch serve`.
"""

__version__ = "0.3.0"

# Public SDK surface, loaded lazily (PEP 562) so bare `import hunch` stays dependency-free:
# touching any of these names pulls in pyobjc via the real modules.
_LAZY = {
    "Hunch": ".sdk",
    "Agent": ".agent",
    "AgentResult": ".agent",
    "HunchError": ".gate",
    "ApprovalDenied": ".gate",
    "AccessibilityNotGranted": ".gate",
    "WebNotOpen": ".gate",
    "StaleRef": ".local_mac",
}


def __getattr__(name):
    if name in _LAZY:
        from importlib import import_module
        return getattr(import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module 'hunch' has no attribute {name!r}")


def __dir__():
    return sorted([*globals(), *_LAZY])
