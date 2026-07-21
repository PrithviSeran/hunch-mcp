"""Hunch credential store — macOS Keychain-backed.

The user's site emails/passwords live in the login Keychain so Hunch can USE them to sign into
sites WITHOUT the values ever entering the agent/LLM context or logs:

  - WRITE path: `hunch creds add` (CLI, interactive) — or an embedding app's own settings UI —
    calls set_credential(). A login may be BOUND to domain(s) at add-time (set_domains); the
    web_fill tools then refuse to type it into any other site (phishing/injection protection).
  - READ path: exactly one MCP tool (web_fill_login) calls get_credential() and types the secret
    straight into the page over CDP, returning only a status. The value never goes back to the model.
  - The agent only ever sees SERVICE NAMES (list_services()), never usernames or passwords.

Secrets are stored as a generic-password item in the Keychain (service label below), with the
per-site name as the account and a small JSON blob as the secret — {username,password} for logins,
{kind:"secret", secret} for single protected values (API keys / tokens, filled by web_fill_secret).
Only the list of service *names* is kept outside the Keychain (a tiny index file, no secrets).

NAMESPACING (developer-first): every public function takes namespace=None. None -> the personal
store (the MCP server / `hunch creds` CLI). A namespace string (an app's id, e.g.
"com.acme.mailbot") -> that app's OWN Keychain service and metadata files under
~/.hunch/apps/<ns>/ — two apps built on the SDK can never see or fill each other's credentials.

Note: set_credential passes the secret as a `security` argv, briefly visible to local `ps`. That's a
local-only exposure (never reaches the LLM); moving it to a stdin/pty feed is a future hardening.
"""
import json
import os
import subprocess

_KC_SERVICE = "com.hunch.credentials"      # personal Keychain generic-password 'service'


def _service(ns=None):
    return f"{_KC_SERVICE}.{ns}" if ns else _KC_SERVICE


def _meta_path(basename, ns=None):
    root = os.path.expanduser(f"~/.hunch/apps/{ns}" if ns else "~/.hunch")
    return os.path.join(root, basename)


# Kind lives OUTSIDE the Keychain on purpose: reading it from the item blob would decrypt secret
# data just to draw a UI badge, which triggers a Keychain password prompt whenever the reading
# binary isn't in the item's ACL (e.g. every rebuilt dev app).


def _read_json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_index(ns=None):
    return _read_json(_meta_path("creds_index.json", ns), [])


def _write_index(names, ns=None):
    _write_json(_meta_path("creds_index.json", ns), sorted(set(names)))


def list_services(namespace=None):
    """Service names only — the sole credential info that is safe to show the agent."""
    return _read_index(namespace)


def has(name, namespace=None):
    return (name or "").strip() in _read_index(namespace)


def _read_kinds(ns=None):
    return _read_json(_meta_path("creds_kinds.json", ns), {})


def _write_kinds(kinds, ns=None):
    _write_json(_meta_path("creds_kinds.json", ns), kinds)


def _store_blob(name, blob, kind, ns=None):
    subprocess.run(["security", "add-generic-password", "-U",
                    "-s", _service(ns), "-a", name, "-w", blob],
                   check=True, capture_output=True, text=True)
    names = _read_index(ns)
    if name not in names:
        names.append(name)
        _write_index(names, ns)
    kinds = _read_kinds(ns)
    if kinds.get(name) != kind:
        kinds[name] = kind
        _write_kinds(kinds, ns)


def _read_blob(name, ns=None):
    try:
        r = subprocess.run(["security", "find-generic-password",
                            "-s", _service(ns), "-a", (name or "").strip(), "-w"],
                           check=True, capture_output=True, text=True)
        return json.loads(r.stdout.strip() or "{}")
    except Exception:
        return {}


def set_credential(name, username, password, namespace=None):
    """Store/replace a username+password login in the Keychain (values go straight to the OS keychain)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("empty service name")
    _store_blob(name, json.dumps({"username": username or "", "password": password or ""}),
                "login", namespace)


def set_secret(name, secret, namespace=None):
    """Store/replace a single protected value (API key, token) — just a service name + the secret."""
    name = (name or "").strip()
    if not name:
        raise ValueError("empty service name")
    if not secret:
        raise ValueError("empty secret")
    _store_blob(name, json.dumps({"kind": "secret", "secret": secret}), "secret", namespace)


def kind_of(name, namespace=None):
    """'secret' (API key / token) or 'login' (username+password). Read from the kinds file, NOT
    the Keychain — no secret data is touched, so no Keychain prompt. Unlisted names are logins."""
    return "secret" if _read_kinds(namespace).get((name or "").strip()) == "secret" else "login"


def get_credential(name, namespace=None):
    """Read (username, password) from the Keychain. CALLERS MUST NOT return this to the agent/LLM —
    only web_fill_login uses it, to type straight into the page."""
    d = _read_blob(name, namespace)
    return d.get("username", ""), d.get("password", "")


def get_secret(name, namespace=None):
    """Read a secret-kind value (API key / token). Same rule as get_credential: the value must go
    straight into a page/app, NEVER back to the agent/LLM."""
    d = _read_blob(name, namespace)
    return d.get("secret", "") if d.get("kind") == "secret" else ""


def username_of(name, namespace=None):
    """The stored username/email for display in the Settings UI (native, never sent to the agent)."""
    return get_credential(name, namespace)[0]


def delete_credential(name, namespace=None):
    name = (name or "").strip()
    subprocess.run(["security", "delete-generic-password", "-s", _service(namespace), "-a", name],
                   check=False, capture_output=True, text=True)
    _write_index([n for n in _read_index(namespace) if n != name], namespace)
    kinds = _read_kinds(namespace)
    if name in kinds:
        del kinds[name]
        _write_kinds(kinds, namespace)
    remove_domains(name, namespace)


def _read_domains(ns=None):
    return _read_json(_meta_path("creds_domains.json", ns), {})


def _write_domains(domains, ns=None):
    _write_json(_meta_path("creds_domains.json", ns), domains)


def _norm_domain(d):
    """'https://www.Accounts.Google.com:443/x' -> 'accounts.google.com' (drop scheme/port/path/www)."""
    d = (d or "").strip().lower()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0].split(":", 1)[0]
    return d[4:] if d.startswith("www.") else d


def domains_of(name, namespace=None):
    """Domains a credential is bound to ([] = unbound, fills anywhere — legacy behavior)."""
    return _read_domains(namespace).get((name or "").strip(), [])


def set_domains(name, domains, namespace=None):
    name = (name or "").strip()
    cleaned = sorted({d for d in (_norm_domain(x) for x in (domains or [])) if d})
    all_domains = _read_domains(namespace)
    if cleaned:
        all_domains[name] = cleaned
    else:
        all_domains.pop(name, None)
    _write_domains(all_domains, namespace)


def remove_domains(name, namespace=None):
    all_domains = _read_domains(namespace)
    if (name or "").strip() in all_domains:
        del all_domains[(name or "").strip()]
        _write_domains(all_domains, namespace)
