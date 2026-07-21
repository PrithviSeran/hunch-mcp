"""errors.py — the SDK's exception types, dependency-free.

Split out of gate.py so light modules (auth, cli) can raise/catch them without
pulling in pyobjc; gate re-exports them, so `from .gate import HunchError`
everywhere else keeps working.
"""


class HunchError(Exception):
    """Base for errors raised by the Hunch SDK layer."""


class ApprovalDenied(HunchError):
    """The user declined a consent dialog (or a gate refused the action)."""


class AccessibilityNotGranted(HunchError, PermissionError):
    """This process is not trusted for Accessibility, so tree reads/actions can't work."""


class WebNotOpen(HunchError):
    """A .web method was called before .web.open()."""
