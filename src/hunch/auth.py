"""auth.py — the EXPLICIT sign-in surface for the agent loop's subscription backend.

Nothing about subscription auth hides inside the agent module, and the surface is
PUBLIC API, not just a CLI: developers embedding hunch-sdk drive sign-in from
their own code —

    import hunch
    st = hunch.auth.status()          # AuthStatus: source / email / plan
    if not st.subscription_ready:
        hunch.login()                 # browser OAuth (or login(token=...) headless)

`hunch login` / `hunch logout` / `hunch login --status` are thin CLI callers of
the same functions, and the agent loop consults the same one resolver
(`resolve()`). Resolution order — first hit wins:

  1. ANTHROPIC_API_KEY          -> metered API backend (anthropic SDK)
  2. CLAUDE_CODE_OAUTH_TOKEN    -> subscription (explicit env token)
  3. Claude Code keychain login -> subscription (the user's own Claude Code
                                   sign-in; the bundled CLI reads it by itself)
  4. com.hunch.claude-token     -> subscription (token saved via
                                   `hunch login --token`; shared with the Hunch
                                   menu-bar app)

Like cli.py this module is stdlib-only, so `hunch login --status` works even
when optional deps are missing.
"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from .errors import HunchError

HUNCH_TOKEN_SERVICE = "com.hunch.claude-token"
CLAUDE_CODE_SERVICE = "Claude Code-credentials"


# ── keychain (macOS `security`) ────────────────────────────────────────────────

def _keychain_read(service):
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                       capture_output=True, text=True)
    val = (r.stdout or "").strip()
    return val if r.returncode == 0 and val else None


def _keychain_present(service):
    return subprocess.run(["security", "find-generic-password", "-s", service],
                          capture_output=True).returncode == 0


def _keychain_write(service, value):
    subprocess.run(["security", "add-generic-password", "-U", "-s", service,
                    "-a", "hunch", "-w", value], capture_output=True, check=True)


def _keychain_delete(service):
    return subprocess.run(["security", "delete-generic-password", "-s", service],
                          capture_output=True).returncode == 0


# ── the claude CLI (bundled with claude-agent-sdk, or a system install) ────────

def claude_cli():
    """Path to a `claude` CLI: the one bundled inside claude-agent-sdk if the
    [subscription] extra is installed, else a system install, else None."""
    try:
        import claude_agent_sdk
        from pathlib import Path
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled"
        for cand in sorted(bundled.glob("claude*")):
            if os.access(cand, os.X_OK):
                return str(cand)
    except ImportError:
        pass
    return shutil.which("claude")


def claude_login_details(cli=None):
    """{'email': ..., 'subscriptionType': ...} for an existing Claude Code login,
    or None. Purely informational — auth itself never needs this."""
    cli = cli or claude_cli()
    if not cli:
        return None
    try:
        r = subprocess.run([cli, "auth", "status", "--json"],
                           capture_output=True, text=True, timeout=15)
        info = json.loads(r.stdout or "{}")
        return info if info.get("loggedIn") else None
    except Exception:
        return None


# ── resolution (single source of truth; the agent loop calls this too) ─────────

def resolve():
    """Which credential the agent loop would use, as
    (source, human_description) — source is one of 'api_key', 'env_token',
    'claude_code', 'hunch_token', or None."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key", "ANTHROPIC_API_KEY env var (metered API)"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "env_token", "CLAUDE_CODE_OAUTH_TOKEN env var (Claude subscription)"
    if _keychain_present(CLAUDE_CODE_SERVICE):
        return "claude_code", "Claude Code sign-in in the Keychain (Claude subscription)"
    if _keychain_read(HUNCH_TOKEN_SERVICE):
        return "hunch_token", "token saved by `hunch login --token` (Claude subscription)"
    return None, "no credentials — run `hunch login` or set ANTHROPIC_API_KEY"


def subscription_available():
    """True when the subscription backend has a usable credential."""
    return resolve()[0] in ("env_token", "claude_code", "hunch_token")


def ensure_subscription_env():
    """Make the subscription credential visible to claude-agent-sdk subprocesses.
    An env token or a Claude Code keychain login needs nothing (the CLI reads
    both itself); a Hunch-saved token is exported as CLAUDE_CODE_OAUTH_TOKEN."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    if _keychain_present(CLAUDE_CODE_SERVICE):
        return
    token = _keychain_read(HUNCH_TOKEN_SERVICE)
    if token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token


# ── public SDK surface (also exported as hunch.login / hunch.logout) ───────────

@dataclass
class AuthStatus:
    """What `hunch login --status` prints, as data. source is one of 'api_key',
    'env_token', 'claude_code', 'hunch_token', or None (no credentials)."""
    source: str | None
    description: str
    email: str | None = None
    plan: str | None = None

    @property
    def subscription_ready(self):
        """True when the subscription backend can run without further sign-in."""
        return self.source in ("env_token", "claude_code", "hunch_token")


def status(details=True):
    """The credential the agent loop would use, as an AuthStatus. details=True also
    asks the claude CLI who the subscription sign-in belongs to (email/plan)."""
    source, desc = resolve()
    email = plan = None
    if details and source in ("env_token", "claude_code", "hunch_token"):
        info = claude_login_details()
        if info:
            email, plan = info.get("email"), info.get("subscriptionType")
    return AuthStatus(source, desc, email, plan)


def login(token=None):
    """Sign in for the subscription backend, from Python. Already signed in -> no-op.
    token=None runs the interactive browser OAuth (needs a TTY-ish environment);
    token="sk-ant-oat..." stores a long-lived token (from `claude setup-token`) in
    the Keychain — the headless path. Returns the resulting AuthStatus; raises
    HunchError when sign-in cannot complete."""
    if token:
        ok, msg = save_token(token)
        if not ok:
            raise HunchError(msg)
        return status()
    st = status(details=False)
    if st.subscription_ready:
        return status()
    ok, msg = interactive_login()
    if not ok:
        raise HunchError(msg)
    return status()


# ── login / logout flows (shared by the CLI and login() above) ─────────────────

def interactive_login():
    """Run the browser OAuth sign-in via the claude CLI (inherited stdio).
    Returns (ok, message)."""
    cli = claude_cli()
    if not cli:
        return False, ("no `claude` CLI available to run the sign-in flow — install the "
                       "subscription extra first: pip install 'hunch-sdk[agent]' "
                       "(or install Claude Code)")
    r = subprocess.run([cli, "auth", "login", "--claudeai"])
    if r.returncode != 0:
        return False, "sign-in did not complete (the login flow exited with an error)."
    info = claude_login_details(cli) or {}
    who = info.get("email", "your Anthropic account")
    plan = info.get("subscriptionType")
    return True, f"signed in as {who}" + (f" ({plan} plan)" if plan else "")


def save_token(token):
    """Store a long-lived OAuth token (from `claude setup-token`) in the Keychain
    under com.hunch.claude-token. Returns (ok, message)."""
    token = (token or "").strip()
    if not token:
        return False, "empty token — nothing saved."
    _keychain_write(HUNCH_TOKEN_SERVICE, token)
    return True, (f"token saved to the macOS Keychain ({HUNCH_TOKEN_SERVICE}). "
                  "The agent loop will use your Claude subscription.")


def logout():
    """Remove the Hunch-saved token. Never touches the Claude Code sign-in or env
    vars — reports what (if anything) still authenticates. Returns messages list."""
    msgs = []
    if _keychain_delete(HUNCH_TOKEN_SERVICE):
        msgs.append(f"removed the Hunch-saved token ({HUNCH_TOKEN_SERVICE}) from the Keychain.")
    else:
        msgs.append("no Hunch-saved token to remove.")
    source, desc = resolve()
    if source == "claude_code":
        msgs.append("note: your Claude Code sign-in still authenticates the agent loop — "
                    "it belongs to Claude Code, so sign out there (`claude auth logout` or "
                    "/logout inside Claude Code) if you want it gone.")
    elif source in ("api_key", "env_token"):
        msgs.append(f"note: {desc} is still set in this shell.")
    return msgs
