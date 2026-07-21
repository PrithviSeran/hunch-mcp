"""gate.py — Hunch's consent + safety layer, shared by the MCP server and the Python SDK.

Everything here used to live at module scope in server.py. It is reshaped around a `Gate`
instance so consent state (the short screen-approval window, the confirm mode) is held
per-caller instead of per-process: the MCP server keeps one module-level Gate, and every
`Hunch` SDK instance gets its own. Policy resolution is unchanged — `Gate.enabled` defers
to policy.gate_enabled, so ~/.hunch/config.json and HUNCH_NO_INTERNAL_GATE keep working
exactly as before; `confirm="off"` is an additional per-instance auto-approve for SDK
callers, without touching the env or the config file.

This module must never import server.py (module-load side effects) or the MCP SDK.
"""
import os
import time
import subprocess

from AppKit import NSWorkspace

from . import policy
from .local_mac import set_focus_reason, suppress_focus_notice

# Exceptions live in errors.py (dependency-free, importable by auth/cli without pyobjc);
# re-exported here so `from .gate import HunchError` keeps working everywhere.
from .errors import HunchError, ApprovalDenied, AccessibilityNotGranted, WebNotOpen  # noqa: F401


def as_str(s):
    """Quote a Python string as an AppleScript string literal (escape \\ and ", keep UTF-8)."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# One user approval covers the immediate follow-through (the switch they just allowed), so the
# agent isn't re-asked — and the user isn't re-notified — for the same event. Short-lived on
# purpose: it is consent for THIS action, not a session-wide waiver.
APPROVAL_WINDOW_S = 15


class Gate:
    """Per-caller consent state + dialogs. confirm="dialog" pops click-to-approve dialogs per
    the policy gates; confirm="off" auto-approves everything for this instance only."""

    def __init__(self, confirm="dialog"):
        if confirm not in ("dialog", "off"):
            raise ValueError(f"confirm must be 'dialog' or 'off', not {confirm!r}")
        self.confirm = confirm
        self._screen_ok_until = 0.0

    def enabled(self, category):
        """Is the gate for `category` active for this caller? Single choke point for the
        per-instance bypass; otherwise env + config flow through policy unchanged."""
        if self.confirm == "off":
            return False
        return policy.gate_enabled(category)

    def confirm_dialog(self, prompt, timeout=180, screen_approval=True):
        """Pop a real click-to-approve dialog with a 'Go ahead' button and BLOCK until the user
        answers (or `timeout`). Returns True only if they click Go ahead. This is how Hunch gets
        consent to take over the screen — one click, no typing. (A true inline notification-button
        would require Hunch to be a signed app bundle using UserNotifications; a Python MCP can't
        post those, so this is a dialog.)

        `screen_approval=True` (any screen-related dialog): an approval also counts as consent for
        the focus switch that follows — the app_to_front gate won't re-ask, and the focus-switch
        notification is suppressed, for a short window. Pass False for non-screen dialogs
        (risky AppleScript) so approving a script doesn't quietly authorize a screen takeover."""
        script = (f'tell application "System Events" to display dialog {as_str(prompt)} '
                  f'with title "Hunch wants your screen" buttons {{"Cancel", "Go ahead"}} '
                  f'default button "Go ahead" cancel button "Cancel" with icon caution '
                  f'giving up after {timeout}')
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                               timeout=timeout + 5)
            ok = "button returned:Go ahead" in (r.stdout or "")
        except Exception:
            return False
        if ok and screen_approval:
            self.mark_screen_approval()
        return ok

    def mark_screen_approval(self):
        self._screen_ok_until = time.monotonic() + APPROVAL_WINDOW_S
        suppress_focus_notice(APPROVAL_WINDOW_S)

    def screen_approved(self):
        return time.monotonic() < self._screen_ok_until

    def front_gate(self, name, reason=""):
        """None if bringing `name` to the front may proceed, else a refusal message for the agent.
        Asks the user first (gates.app_to_front) unless they already approved a screen dialog
        moments ago, the gate is off, or the app is already frontmost (no real switch)."""
        if self.screen_approved() or not self.enabled("app_to_front"):
            return None
        try:
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front and front.localizedName() == name:
                return None
        except Exception:
            pass
        why = f" — {reason}" if reason else ""
        if self.confirm_dialog(f"Hunch wants to bring “{name}” to the front{why}. Allow?"):
            return None
        return (f"User did NOT approve bringing “{name}” to the front. Work on it in the "
                "background instead (snapshot / web tools are focus-free), or ask the user "
                "how they'd like to proceed.")


def check_focus_steal(computer, actions, gate, confirm=False, reason=""):
    """Gate the focus-stealing actions in an `act` batch (key / click_xy / ref-less type — they
    use the shared keyboard/mouse and need the app frontmost). Returns None if the batch may
    run, else the refusal message. Sets the focus reason so the focus warning explains itself."""
    stealing = [a for a in actions if computer._is_shared_input(a)]
    if stealing:
        set_focus_reason(reason)   # the focus-switch warning tells the user WHY
    if stealing and not computer.simultaneous and not confirm and gate.enabled("focus_steal"):
        kinds = ", ".join(sorted({a.get("action") for a in stealing}))
        why = f" — {reason}" if reason else ""
        if not gate.confirm_dialog(f"Hunch wants to take over your screen ({kinds} on "
                                   f"“{computer.app}”){why}. Allow?"):
            return (f"User did NOT approve — skipped {len(stealing)} focus-stealing action(s) "
                    f"[{kinds}]. Ask the user how they'd like to proceed.")
    return None


# Only GENUINELY dangerous / irreversible / OUTWARD verbs get Hunch's own click-to-approve.
# Creating things (make new draft/note/event/reminder), reading, and playback are benign and
# reversible, so they run silently — the CLIENT (Claude Code / Desktop) is the primary permission
# gate; this internal gate is just a content-aware safety net for the catastrophic cases.
SHELL_AS = ("do shell script",)
DESTRUCTIVE_AS = ("delete ", "remove ", "erase", "empty trash",
                  "send ", "shut down", "restart", "log out", "logout", "eject")


def applescript_category(script):
    """Risk class of an AppleScript: "shell", "destructive_applescript", or None (benign)."""
    low = script.lower()
    return ("shell" if any(k in low for k in SHELL_AS)
            else "destructive_applescript" if any(k in low for k in DESTRUCTIVE_AS)
            else None)


def applescript_hint(out):
    """Permission-aware suffix for an AppleScript error (TCC denials), or ""."""
    if "assistive access" in out.lower() or "-25211" in out:
        return (" — macOS blocked this: the app hosting Hunch lacks the Accessibility "
                "permission. Tell the user: System Settings → Privacy & Security → "
                "Accessibility, enable the MCP host app (toggle off/on if already listed), "
                "then restart it. `hunch doctor` explains.")
    if "not authorized" in out.lower() or "-1743" in out:
        return (" — Automation consent missing for the target app. macOS shows a one-time "
                "'allow control' prompt on first use; if it was denied, tell the user to "
                "re-enable it in System Settings → Privacy & Security → Automation.")
    return ""


HUNCH_DIR = os.path.realpath(os.path.expanduser("~/.hunch"))


def protected(p):
    """True for paths inside ~/.hunch — Hunch's own gate policy + credential metadata. The agent
    must not be able to edit its own permissions; changes go through the `hunch` CLI a human runs.
    realpath on both sides so a symlink can't launder the target."""
    rp = os.path.realpath(os.path.abspath(os.path.expanduser(str(p))))
    return rp == HUNCH_DIR or rp.startswith(HUNCH_DIR + os.sep)


def domain_mismatch(service, current_url):
    """None if `current_url`'s host is allowed for `service`, else a refusal message.
    A credential bound at add-time (`hunch creds add --domain`) only ever gets typed into that
    site — the last line of defense when a prompt-injected agent lands on a look-alike page."""
    from urllib.parse import urlparse
    from .creds import domains_of
    allowed = domains_of(service)
    if not allowed:
        return None   # unbound (legacy / user chose blank): fills anywhere, as before
    try:
        host = (urlparse(current_url).hostname or "").lower()
    except Exception:
        host = ""
    if any(host == d or host.endswith("." + d) for d in allowed):
        return None
    return (f"REFUSED: '{service}' is bound to {allowed} but the current page is '{host or 'unknown'}'. "
            "Hunch won't type this credential into a different site (phishing/prompt-injection "
            "protection). If this is legitimate, the user can re-bind it in a terminal: "
            f"hunch creds add {service} --domain {host or '<domain>'}")
