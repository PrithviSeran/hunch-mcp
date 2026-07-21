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


from dataclasses import dataclass  # noqa: E402  (kept below the exceptions on purpose)


@dataclass(frozen=True)
class ConsentRequest:
    """What a custom confirm callback receives: Hunch(confirm=my_callback) is called with
    one of these before every gated action and must return truthy to approve. category is
    the gate name ('focus_steal', 'app_to_front', 'shell', 'destructive_applescript', or
    'focus' for an agent request_focus); prompt is the human-ready question; detail carries
    extra context (e.g. the AppleScript preview)."""
    prompt: str
    category: str = ""
    detail: str = ""
