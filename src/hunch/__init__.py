"""Hunch — drive your Mac focus-free over MCP.

This module is intentionally dependency-free: the CLI (config, creds, connect,
doctor) must work even when pyobjc or the MCP SDK are missing, so nothing here
may import them. The server loads lazily via `hunch serve`.
"""

__version__ = "0.1.2"
