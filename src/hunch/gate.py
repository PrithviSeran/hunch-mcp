"""gate.py — Hunch's consent + safety layer, shared by the MCP server and the Python SDK.

UNIFORM, instance-owned semantics (the developer-first contract): every Gate carries its
OWN policy and consent surface. The SDK never reads ~/.hunch/config.json or the
HUNCH_NO_INTERNAL_GATE env by default — those personal-machine behaviors belong to the
MCP server, which (as just another app built on the SDK) passes policy="personal" to get
the live file-backed resolver. Consent renders as an osascript dialog by default, or
through the app's own UI via a confirm callback, branded with the app's name.

This module must never import server.py (module-load side effects) or the MCP SDK.
"""
import os
import time
import subprocess

from . import policy
from .local_mac import set_focus_reason, suppress_focus_notice, _frontmost
from .notify import as_str

# Exceptions live in errors.py (dependency-free, importable by auth/cli without pyobjc);
# re-exported here so `from .gate import HunchError` keeps working everywhere.
from .errors import (HunchError, ApprovalDenied, AccessibilityNotGranted, WebNotOpen,  # noqa: F401
                     ConsentRequest)




# One user approval covers the immediate follow-through (the switch they just allowed), so the
# agent isn't re-asked — and the user isn't re-notified — for the same event. Short-lived on
# purpose: it is consent for THIS action, not a session-wide waiver.
APPROVAL_WINDOW_S = 15


class Gate:
    """Per-caller consent state + policy + dialogs — the instance-owned safety layer.

    confirm: "dialog" pops click-to-approve dialogs; "off" auto-approves everything for
    this instance; a callable routes consent through the app's own UI — it receives a
    ConsentRequest and returns truthy to approve (an exception counts as a denial).

    policy: who decides which gates are active. None -> ALL gates on (the uniform
    developer-first default); a dict ({"gates": {...}, "auto_approve_all": bool}) ->
    instance-owned, never reads global state; a callable(category)->bool -> custom
    resolver; "personal" -> the live ~/.hunch/config.json resolver incl. the
    HUNCH_NO_INTERNAL_GATE env kill-switch (what the MCP server passes).

    app_name brands every dialog ("Acme Mailbot wants to ..." instead of "Hunch")."""

    def __init__(self, confirm="dialog", app_name="Hunch", policy=None):
        if confirm not in ("dialog", "off") and not callable(confirm):
            raise ValueError(f"confirm must be 'dialog', 'off', or a callable, not {confirm!r}")
        if not (policy is None or policy == "personal" or callable(policy)
                or isinstance(policy, dict)):
            raise ValueError(f"policy must be None, a dict, a callable, or 'personal', "
                             f"not {policy!r}")
        self.confirm = confirm
        self.app_name = app_name or "Hunch"
        self._policy = policy
        self._screen_ok_until = 0.0

    def enabled(self, category):
        """Is the gate for `category` active for this caller? Single choke point:
        the per-instance bypass first, then THIS instance's policy — nothing global
        unless policy='personal' opted in."""
        if self.confirm == "off":
            return False
        p = self._policy
        if p is None:
            return True                     # uniform default: every gate on
        if p == "personal":
            return policy.gate_enabled(category)   # ~/.hunch/config.json + env kill-switch
        if callable(p):
            return bool(p(category))
        if p.get("auto_approve_all"):
            return False
        return bool(p.get("gates", {}).get(category, True))

    def confirm_dialog(self, prompt, timeout=180, screen_approval=True, category="", detail=""):
        """Ask the human for consent and BLOCK until they answer. Returns True only on
        approval. Routing: a callable confirm gets ConsentRequest(prompt, category,
        detail) — its exceptions count as denial; "off" auto-approves; "dialog" pops a
        real osascript dialog titled with app_name. (A true inline notification-button
        would require a signed app bundle using UserNotifications; a Python process
        can't post those, so the built-in fallback is a dialog.)

        `screen_approval=True` (any screen-related dialog): an approval also counts as
        consent for the focus switch that follows — the app_to_front gate won't re-ask,
        and the focus-switch notification is suppressed, for a short window. Pass False
        for non-screen dialogs (risky AppleScript) so approving a script doesn't quietly
        authorize a screen takeover."""
        if callable(self.confirm):
            try:
                ok = bool(self.confirm(ConsentRequest(prompt, category, detail)))
            except Exception:
                ok = False                  # a broken callback must fail CLOSED
        elif self.confirm == "off":
            ok = True                       # instance-wide auto-approve
        else:
            script = (f'tell application "System Events" to display dialog {as_str(prompt)} '
                      f'with title {as_str(self.app_name + " wants your screen")} '
                      f'buttons {{"Cancel", "Go ahead"}} '
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
        if _frontmost()[0] == name:
            return None
        why = f" — {reason}" if reason else ""
        if self.confirm_dialog(f"{self.app_name} wants to bring “{name}” to the front{why}. "
                               "Allow?", category="app_to_front", detail=name):
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
        if not gate.confirm_dialog(f"{gate.app_name} wants to take over your screen ({kinds} on "
                                   f"“{computer.app}”){why}. Allow?",
                                   category="focus_steal", detail=kinds):
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


def applescript_empty_hint(script):
    """Why a script that SUCCEEDED returned nothing — or "" if there's no known reason.

    An empty result is the worst kind of answer: the script ran, nothing failed, and the agent
    can't tell 'no windows' from 'this question can't be answered this way'. The common case is
    asking System Events for a Chromium/Electron app's windows: those apps vend their UI to the
    AX API, not to System Events' scripting bridge, so `count of windows` is a truthful-looking 0
    no matter how many windows are on screen. Retrying the same script (the natural next move,
    and the one that wastes turns) can never return anything else."""
    s = (script or "").lower()
    if "system events" not in s:
        return ""
    if "window" in s and "process" in s:
        return ("\n[!] This returned EMPTY, and re-running it will too. System Events only sees "
                "windows of apps that expose them to its scripting bridge — a Chromium/Electron "
                "app (Cursor, VS Code, Slack, Discord, Chrome) reports 0 windows here even with "
                "many open. Read its windows through the AX layer instead: snapshot(app=...) for "
                "the focused window, or web_tabs after web_open for the CDP-driven copy.")
    return ""


HUNCH_DIR = os.path.realpath(os.path.expanduser("~/.hunch"))


def protected(p):
    """True for paths inside ~/.hunch — Hunch's own gate policy + credential metadata. The agent
    must not be able to edit its own permissions; changes go through the `hunch` CLI a human runs.
    realpath on both sides so a symlink can't launder the target."""
    rp = os.path.realpath(os.path.abspath(os.path.expanduser(str(p))))
    return rp == HUNCH_DIR or rp.startswith(HUNCH_DIR + os.sep)


def domain_mismatch(service, current_url, app_name="Hunch", namespace=None):
    """None if `current_url`'s host is allowed for `service`, else a refusal message.
    A credential bound at add-time (`hunch creds add --domain`) only ever gets typed into that
    site — the last line of defense when a prompt-injected agent lands on a look-alike page."""
    from urllib.parse import urlparse
    from .creds import domains_of
    allowed = domains_of(service, namespace)
    if not allowed:
        return None   # unbound (legacy / user chose blank): fills anywhere, as before
    try:
        host = (urlparse(current_url).hostname or "").lower()
    except Exception:
        host = ""
    if any(host == d or host.endswith("." + d) for d in allowed):
        return None
    return (f"REFUSED: '{service}' is bound to {allowed} but the current page is '{host or 'unknown'}'. "
            f"{app_name} won't type this credential into a different site (phishing/prompt-injection "
            "protection). If this is legitimate, the user can re-bind it in a terminal: "
            f"hunch creds add {service} --domain {host or '<domain>'}")
