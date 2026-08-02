"""auth.py — the EXPLICIT sign-in surface for the agent loop's subscription backend.

Nothing about subscription auth hides inside the agent module: this is PUBLIC
Python API, and developers drive sign-in from their own code —

    import hunch
    st = hunch.auth.status()          # AuthStatus: source / email / plan
    if not st.subscription_ready:
        hunch.login()                 # browser OAuth (or login(token=...) headless)

The agent loop consults the same one resolver (`resolve()`), and `hunch doctor`
prints it. Resolution order — first hit wins:

  1. ANTHROPIC_API_KEY          -> metered API backend (anthropic SDK)
  2. CLAUDE_CODE_OAUTH_TOKEN    -> subscription (explicit env token)
  3. Claude Code keychain login -> subscription (the user's own Claude Code
                                   sign-in; the bundled CLI reads it by itself)
  4. com.hunch.claude-token     -> subscription (token saved via
                                   hunch.login(token=...); shared with the Hunch
                                   menu-bar app)

Like cli.py this module is stdlib-only, so status/login work even when the
optional LLM SDKs are missing.
"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from .errors import HunchError

HUNCH_TOKEN_SERVICE = "com.hunch.claude-token"
CLAUDE_CODE_SERVICE = "Claude Code-credentials"


def _token_service(app_id=None):
    """Keychain service for the saved OAuth token. app_id (pure namespacing) gives an
    app its own slot so two apps built on the SDK can't log each other out."""
    return f"com.hunch.token.{app_id}" if app_id else HUNCH_TOKEN_SERVICE


# ── injected credentials (developer-first: pass exactly the credential you manage) ─

@dataclass(frozen=True)
class ApiKey:
    """A metered Anthropic API key, injected per-instance: Hunch(auth=ApiKey("sk-ant-...")).
    Forces the agent loop's 'api' backend; ambient resolution is skipped entirely."""
    value: str

    def __repr__(self):  # never leak the key into logs/tracebacks
        return "ApiKey('sk-ant-…redacted')"


@dataclass(frozen=True)
class OAuthToken:
    """A Claude-subscription OAuth token (e.g. from `claude setup-token`), injected
    per-instance: Hunch(auth=OAuthToken("sk-ant-oat-...")). Forces the 'subscription'
    backend; ambient resolution is skipped entirely."""
    value: str

    def __repr__(self):
        return "OAuthToken('sk-ant-oat-…redacted')"


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

def resolve(app_id=None):
    """Which credential the agent loop would use, as
    (source, human_description) — source is one of 'api_key', 'env_token',
    'claude_code', 'hunch_token', or None. app_id namespaces the saved-token slot."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key", "ANTHROPIC_API_KEY env var (metered API)"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "env_token", "CLAUDE_CODE_OAUTH_TOKEN env var (Claude subscription)"
    if _keychain_present(CLAUDE_CODE_SERVICE):
        return "claude_code", "Claude Code sign-in in the Keychain (Claude subscription)"
    if _keychain_read(_token_service(app_id)):
        return "hunch_token", "token saved by hunch.login(token=...) (Claude subscription)"
    return None, "no credentials — call hunch.login() or set ANTHROPIC_API_KEY"


def subscription_available(app_id=None):
    """True when the subscription backend has a usable credential."""
    return resolve(app_id)[0] in ("env_token", "claude_code", "hunch_token")


def subscription_token(app_id=None):
    """The token the claude CLI subprocess needs handed to it, or None when it can
    find the credential by itself (env token in the inherited env, or a Claude Code
    keychain sign-in). NEVER mutates os.environ — the caller passes this via the
    SDK's per-client options.env."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return None
    if _keychain_present(CLAUDE_CODE_SERVICE):
        return None
    return _keychain_read(_token_service(app_id))


# ── public SDK surface (also exported as hunch.login / hunch.logout) ───────────

@dataclass
class AuthStatus:
    """The agent loop's credential situation, as data. source is one of 'api_key',
    'env_token', 'claude_code', 'hunch_token', or None (no credentials)."""
    source: str | None
    description: str
    email: str | None = None
    plan: str | None = None

    @property
    def subscription_ready(self):
        """True when the subscription backend can run without further sign-in."""
        return self.source in ("env_token", "claude_code", "hunch_token")


def status(details=True, app_id=None):
    """The credential the agent loop would use, as an AuthStatus. details=True also
    asks the claude CLI who the subscription sign-in belongs to (email/plan).
    app_id namespaces the saved-token slot (pure naming — same semantics)."""
    source, desc = resolve(app_id)
    email = plan = None
    if details and source in ("env_token", "claude_code", "hunch_token"):
        info = claude_login_details()
        if info:
            email, plan = info.get("email"), info.get("subscriptionType")
    return AuthStatus(source, desc, email, plan)


def login(token=None, app_id=None):
    """Sign in for the subscription backend, from Python. Already signed in -> no-op.
    token=None runs the interactive browser OAuth (needs a TTY-ish environment);
    token="sk-ant-oat..." stores a long-lived token (from `claude setup-token`) in
    the Keychain — the headless path. app_id stores to that app's own slot. Returns
    the resulting AuthStatus; raises HunchError when sign-in cannot complete."""
    if token:
        ok, msg = save_token(token, app_id)
        if not ok:
            raise HunchError(msg)
        return status(app_id=app_id)
    st = status(details=False, app_id=app_id)
    if st.subscription_ready:
        return status(app_id=app_id)
    ok, msg = interactive_login()
    if not ok:
        raise HunchError(msg)
    return status(app_id=app_id)


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


def save_token(token, app_id=None):
    """Store a long-lived OAuth token (from `claude setup-token`) in the Keychain
    under the (possibly app-namespaced) Hunch slot. Returns (ok, message)."""
    token = (token or "").strip()
    if not token:
        return False, "empty token — nothing saved."
    service = _token_service(app_id)
    _keychain_write(service, token)
    return True, (f"token saved to the macOS Keychain ({service}). "
                  "The agent loop will use your Claude subscription.")


def logout(app_id=None):
    """Remove the Hunch-saved token (for this app_id's slot only). Never touches the
    Claude Code sign-in or env vars — reports what (if anything) still
    authenticates. Returns messages list."""
    msgs = []
    service = _token_service(app_id)
    if _keychain_delete(service):
        msgs.append(f"removed the saved token ({service}) from the Keychain.")
    else:
        msgs.append("no saved token to remove.")
    source, desc = resolve(app_id)
    if source == "claude_code":
        msgs.append("note: your Claude Code sign-in still authenticates the agent loop — "
                    "it belongs to Claude Code, so sign out there (`claude auth logout` or "
                    "/logout inside Claude Code) if you want it gone.")
    elif source in ("api_key", "env_token"):
        msgs.append(f"note: {desc} is still set in this shell.")
    return msgs


# ── OpenAI Codex auth (the 'codex' backend) ─────────────────────────────────────
# MANUAL authentication only — no ambient credential detection (the same stance the app
# took for Claude, which stopped scavenging a detected login because it expired mid-task).
# Authorize explicitly through the SDK with codex_login(); the codex backend then relies on
# the SDK's stored session, or on an injected hunch.OpenAIKey(...).

def _codex_client():
    """A Codex SDK client for auth operations, or a HunchError if openai-codex is missing."""
    try:
        import openai_codex
    except ImportError as e:
        raise HunchError("Codex auth needs the optional 'openai-codex' package (beta) — "
                         "install it with: pip install --pre openai-codex "
                         "(or: pip install 'hunch-sdk[codex]')") from e
    return openai_codex.Codex()


@dataclass
class CodexAuthStatus:
    """The 'codex' backend's sign-in state, as data (parallels AuthStatus for Claude).
    method is 'chatgpt' | 'api_key' | None."""
    logged_in: bool
    method: str | None = None
    email: str | None = None
    plan: str | None = None


def codex_login():
    """Authorize the 'codex' backend from Python — an interactive ChatGPT/Codex browser
    sign-in that BLOCKS until you finish (needs a local browser). For a remote/headless
    machine use codex_device_login(). Returns the resulting CodexAuthStatus; raises
    HunchError if openai-codex is missing. (Subscription/login only — no API-key path.)"""
    handle = _codex_client().login_chatgpt()   # its auth_url opens a browser
    handle.wait()                              # block until the browser flow completes
    return codex_status()


def codex_device_login():
    """Headless/remote ChatGPT sign-in for the 'codex' backend. Returns the SDK's
    device-code handle exposing .verification_url, .user_code, and .wait(): show the code
    and URL to the user, then call handle.wait() to block until they finish."""
    return _codex_client().login_chatgpt_device_code()


def codex_logout():
    """Sign the 'codex' backend out (clears the SDK's stored Codex session)."""
    _codex_client().logout()
    return True


def codex_status():
    """Who, if anyone, is signed in for the 'codex' backend — an EXPLICIT check (asks the
    SDK's account()), never ambient scavenging. Returns a CodexAuthStatus; logged_in is
    False when openai-codex is missing or no session exists."""
    try:
        codex = _codex_client()
    except HunchError:
        return CodexAuthStatus(False)
    try:
        resp = codex.account()
    except Exception:
        return CodexAuthStatus(False)
    # NB: `requires_openai_auth` is NOT a logged-out signal (it's True even for a signed-in
    # ChatGPT account). Being logged in = a populated account.root (ChatgptAccount/ApiKeyAccount).
    acct = getattr(getattr(resp, "account", None), "root", None)
    kind = type(acct).__name__ if acct is not None else ""
    if kind == "ChatgptAccount":
        plan = getattr(acct, "plan_type", None)
        plan = getattr(plan, "value", plan)          # PlanType enum -> its str value
        return CodexAuthStatus(True, "chatgpt", getattr(acct, "email", None), plan)
    if kind == "ApiKeyAccount":
        return CodexAuthStatus(True, "api_key")
    return CodexAuthStatus(False)
