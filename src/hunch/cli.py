"""The `hunch` command — install-time and daily driver CLI around the MCP server.

Deliberately stdlib-only (argparse/getpass/json): the CLI must work — and give
useful errors — even when the heavy runtime deps (pyobjc, mcp) are broken, so
`hunch.server` is only imported inside `hunch serve` and probed in `hunch doctor`.
"""

import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import __version__, policy

CHROME_PROFILE = os.path.expanduser("~/.hunch/chrome-cdp")
CDP_PORT = 9337

AUTO_APPROVE_WARNING = """\
WARNING — you are disabling ALL of Hunch's confirmation dialogs.

Hunch runs as a background agent: it reads web pages, emails, and documents
whose content you did not write. A malicious page can try to steer the model
(prompt injection). The confirmation dialogs are the last human check between
such content and your shell, your files, and your logged-in sessions.

With auto_approve_all on, nothing asks you before 'do shell script', deletes,
sends, or screen takeovers. Your MCP host's own tool approvals (if enabled)
become the only gate.
"""


def _ok(msg):
    print(f"  PASS  {msg}")


def _warn(msg):
    print(f"  WARN  {msg}")


def _fail(msg):
    print(f"  FAIL  {msg}")
    return True


# ── serve ──────────────────────────────────────────────────────────────────────


def cmd_serve(args):
    try:
        from . import server
    except ImportError as e:
        print(f"hunch serve: cannot load the server ({e}).\n"
              "Run `hunch doctor` to see which dependency is broken "
              "(pip install hunch-mcp pulls them all).", file=sys.stderr)
        return 1
    server.main()
    return 0


# ── config ─────────────────────────────────────────────────────────────────────


def _print_config():
    cfg = policy.load()
    env_override = bool(os.environ.get("HUNCH_NO_INTERNAL_GATE"))
    print(f"config file: {policy.CONFIG_PATH}"
          + ("" if os.path.exists(policy.CONFIG_PATH) else " (not written yet — showing defaults)"))
    for key in policy.CONFIG_KEYS:
        val = policy.get(cfg, key)
        if val is None:
            val = policy.DEFAULT_GATES.get(key.split(".", 1)[-1], False)
        effective = policy.gate_enabled(key.split(".", 1)[-1]) if key.startswith("gates.") else val
        note = ""
        if env_override and key.startswith("gates."):
            note = "  (suppressed: HUNCH_NO_INTERNAL_GATE is set)"
        elif cfg.get("auto_approve_all") and key.startswith("gates."):
            note = "  (suppressed: auto_approve_all is on)"
        state = "on" if val else "off"
        eff = "" if bool(val) == bool(effective) or not key.startswith("gates.") else f" -> effectively {'on' if effective else 'off'}"
        print(f"  {key} = {state}{eff}{note}")


def cmd_config(args):
    if args.action == "show" or args.action is None:
        _print_config()
        return 0
    if args.action == "reset":
        policy.save(policy.default_config())
        print("config reset to defaults (all gates on).")
        return 0
    # set
    key, value = args.key, args.value
    if key not in policy.CONFIG_KEYS:
        print(f"unknown key {key!r} — one of: {', '.join(policy.CONFIG_KEYS)}", file=sys.stderr)
        return 1
    if value not in ("on", "off"):
        print("value must be 'on' or 'off'", file=sys.stderr)
        return 1
    turning_on = value == "on"
    cfg = policy.load()
    if key == "auto_approve_all" and turning_on:
        print(AUTO_APPROVE_WARNING)
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            print("aborted — auto_approve_all left off.")
            return 1
        cfg["auto_approve_warned"] = True
    policy.set_key(cfg, key, turning_on)
    policy.save(cfg)
    print(f"{key} = {value}  (applies immediately, even to a running server)")
    return 0


# ── creds ──────────────────────────────────────────────────────────────────────


def cmd_creds(args):
    from . import creds
    if args.creds_action == "list":
        names = creds.list_services()
        if not names:
            print("no saved credentials. Add one: hunch creds add <service>")
            return 0
        for name in names:
            kind = creds.kind_of(name)
            domains = creds.domains_of(name)
            bound = ", ".join(domains) if domains else "any site (unbound)"
            print(f"  {name}  [{kind}]  domains: {bound}")
        return 0

    if args.creds_action == "remove":
        if not creds.has(args.service):
            print(f"no saved credential named {args.service!r}", file=sys.stderr)
            return 1
        creds.delete_credential(args.service)
        print(f"removed '{args.service}' from the Keychain.")
        return 0

    # add
    service = args.service.strip()
    if not service:
        print("service name required", file=sys.stderr)
        return 1
    if args.secret:
        value = getpass.getpass(f"Secret value for '{service}' (API key/token, hidden): ")
        if not value:
            print("empty secret — nothing saved.", file=sys.stderr)
            return 1
        creds.set_secret(service, value)
    else:
        username = input(f"Username/email for '{service}': ").strip()
        password = getpass.getpass(f"Password for '{service}' (hidden): ")
        creds.set_credential(service, username, password)
    domains = list(args.domain or [])
    if not domains:
        raw = input("Bind to domain(s)? (recommended — comma-separated, e.g. google.com; "
                    "blank = usable on any site): ").strip()
        domains = [d for d in (x.strip() for x in raw.split(",")) if d]
    creds.set_domains(service, domains)
    bound = ", ".join(creds.domains_of(service)) or "any site (unbound)"
    print(f"saved '{service}' to the macOS Keychain. Fillable on: {bound}\n"
          "The value never enters the model's context — agents fill it via "
          f"web_fill_{'secret' if args.secret else 'login'}('{service}').")
    return 0


# ── connect ────────────────────────────────────────────────────────────────────


def _hunch_exe():
    exe = shutil.which("hunch")
    if exe:
        return exe
    # last resort: however we were invoked (absolute path so GUI hosts without a
    # shell PATH can spawn it)
    return str(Path(sys.argv[0]).resolve())


def _merge_json_config(path: Path, entry: dict):
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text() or "{}")
        except ValueError:
            print(f"{path} exists but is not valid JSON — fix it first.", file=sys.stderr)
            return False
        shutil.copy2(path, str(path) + ".bak")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    cfg.setdefault("mcpServers", {})["hunch"] = entry
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, path)
    return True


def cmd_connect(args):
    exe = _hunch_exe()
    entry = {"command": exe, "args": ["serve"]}
    host = args.host

    if host == "claude-code":
        claude = shutil.which("claude")
        if claude:
            r = subprocess.run([claude, "mcp", "add", "--scope", "user", "hunch", "--", exe, "serve"])
            if r.returncode == 0:
                print("added via `claude mcp add` (user scope). Restart Claude Code sessions to pick it up.")
                return 0
            print("`claude mcp add` failed — falling back to project .mcp.json", file=sys.stderr)
        target = Path.cwd() / ".mcp.json"
        if _merge_json_config(target, entry):
            print(f"wrote {target} (project scope — commit it or keep it local). "
                  "Restart Claude Code in this directory.")
            return 0
        return 1

    if host == "claude-desktop":
        target = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    elif host == "cursor":
        target = (Path.cwd() / ".cursor/mcp.json") if args.project else (Path.home() / ".cursor/mcp.json")
    else:
        print(f"unknown host {host!r}", file=sys.stderr)
        return 1

    if not _merge_json_config(target, entry):
        return 1
    print(f"wrote {target} (backup at {target}.bak if it existed).\n"
          f"Restart {host} to pick up the 'hunch' MCP server.")
    return 0


# ── doctor ─────────────────────────────────────────────────────────────────────


def cmd_doctor(args):
    failed = False
    print(f"hunch {__version__} — doctor\n")

    print("Runtime")
    if sys.version_info >= (3, 11):
        _ok(f"python {sys.version.split()[0]}")
    else:
        failed |= _fail(f"python {sys.version.split()[0]} — need >= 3.11")
    for mod, why in [("mcp.server.fastmcp", "MCP SDK"),
                     ("websocket", "websocket-client (web/CDP layer)"),
                     ("AppKit", "pyobjc Cocoa (OS layer)"),
                     ("ApplicationServices", "pyobjc (Accessibility layer)"),
                     ("Quartz", "pyobjc (input/screenshot layer)")]:
        try:
            __import__(mod)
            _ok(f"import {mod} ({why})")
        except Exception as e:
            failed |= _fail(f"import {mod} ({why}): {e}")

    print("\nAccessibility (AX layer)")
    try:
        from ApplicationServices import AXIsProcessTrusted
        if AXIsProcessTrusted():
            _ok("this process is AX-trusted")
        else:
            _warn("this process is NOT AX-trusted")
        print("        note: this reflects the grant of the app running `hunch doctor` "
              "(your terminal). The server inherits the grant of the MCP HOST app "
              "(Claude Desktop / Cursor / ...) — grant Accessibility to THAT app.")
    except Exception as e:
        failed |= _fail(f"AX check unavailable: {e}")

    print("\nAutomation (AppleScript layer)")
    try:
        r = subprocess.run(["osascript", "-e",
                            'tell application "System Events" to count processes'],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            _ok("System Events scriptable (Automation consent granted for this terminal)")
        else:
            _warn(f"osascript probe failed: {(r.stderr or '').strip()[:120]} "
                  "— macOS may prompt for Automation consent on first real use")
    except subprocess.TimeoutExpired:
        _warn("osascript probe timed out — likely waiting on an Automation consent prompt")
    except Exception as e:
        failed |= _fail(f"osascript unavailable: {e}")

    print("\nWeb layer (CDP)")
    chrome = "/Applications/Google Chrome.app"
    if os.path.exists(chrome):
        _ok("Google Chrome installed")
    else:
        _warn("Google Chrome not found in /Applications — the web layer needs it")
    if os.path.isdir(CHROME_PROFILE):
        _ok(f"Hunch browser profile exists ({CHROME_PROFILE})")
    else:
        _warn("no Hunch browser profile yet — run `hunch setup` to create it and sign into your sites")
    try:
        with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=0.3):
            _warn(f"something is already listening on port {CDP_PORT} — fine if it's a Hunch "
                  "browser, otherwise the web layer may conflict")
    except OSError:
        _ok(f"CDP port {CDP_PORT} free")

    print("\nPolicy & credentials")
    cfg = policy.load()
    if os.environ.get("HUNCH_NO_INTERNAL_GATE"):
        _warn("HUNCH_NO_INTERNAL_GATE is set — ALL internal confirmation dialogs are off in this environment")
    if cfg.get("auto_approve_all"):
        _warn("auto_approve_all is ON — no confirmation dialogs. `hunch config set auto_approve_all off` to restore")
    gates = [k for k in policy.DEFAULT_GATES if policy.gate_enabled(k)]
    _ok(f"gates active: {', '.join(gates) if gates else 'none'}")
    try:
        from . import creds
        names = creds.list_services()
        bound = sum(1 for n in names if creds.domains_of(n))
        _ok(f"credentials: {len(names)} saved ({bound} domain-bound)")
    except Exception as e:
        failed |= _fail(f"credential metadata unreadable: {e}")

    if args.notify:
        print("\nNotification test")
        from .notify import ICON, notify
        if shutil.which("terminal-notifier"):
            _ok("terminal-notifier present — notifications wear the Hunch logo")
        else:
            _warn("terminal-notifier not found — notifications fall back to the plain osascript style "
                  "(brew install terminal-notifier to get the logo)")
        notify("Hunch doctor test", "Hunch")
        _ok("test notification fired — did you see it?")

    print("\n" + ("Some checks FAILED — fix the above and re-run." if failed else
                  "No failures. WARNs (if any) explain themselves above."))
    return 1 if failed else 0


# ── setup ──────────────────────────────────────────────────────────────────────


def cmd_setup(args):
    print(f"hunch {__version__} — one-time setup\n")

    print("1) Accessibility (lets Hunch read app UIs and click focus-free)")
    print("   IMPORTANT: macOS grants attach to the APP THAT RUNS THE SERVER — your MCP host")
    print("   (Claude Desktop, Cursor, your terminal for Claude Code) — not to 'hunch' itself.")
    try:
        from ApplicationServices import AXIsProcessTrusted
        trusted = AXIsProcessTrusted()
        print(f"   this terminal is currently {'AX-trusted' if trusted else 'NOT AX-trusted'}.")
    except Exception:
        pass
    if input("   Open the Accessibility settings pane now? [Y/n] ").strip().lower() != "n":
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security"
                        "?Privacy_Accessibility"], check=False)
        print("   -> add/enable your MCP host app (and your terminal, for testing).")

    print("\n2) Automation (lets Hunch drive scriptable apps: Mail, Music, Finder ...)")
    print("   macOS shows a one-time 'allow control' prompt per app, on first use. Triggering")
    print("   the System Events one now:")
    subprocess.run(["osascript", "-e",
                    'tell application "System Events" to count processes'],
                   capture_output=True, text=True, timeout=15)
    print("   (if a consent dialog appeared, click OK — others will appear per app later.)")

    print("\n3) Screen Recording (OPTIONAL — only for the screenshot/vision fallback)")
    if input("   Open the Screen Recording settings pane? [y/N] ").strip().lower() == "y":
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security"
                        "?Privacy_ScreenCapture"], check=False)

    print("\n4) Hunch browser profile (the web layer signs into sites HERE, once, so agents")
    print("   can use them in the background later)")
    if os.path.isdir(CHROME_PROFILE):
        print(f"   profile already exists at {CHROME_PROFILE} — skipping (delete it to redo).")
    elif input("   Launch Chrome on the Hunch profile now to sign into your sites? [Y/n] ").strip().lower() != "n":
        os.makedirs(CHROME_PROFILE, exist_ok=True)
        subprocess.run(["open", "-na", "Google Chrome", "--args",
                        f"--user-data-dir={CHROME_PROFILE}"], check=False)
        input("   -> sign into the sites you want Hunch to use, then press Enter here... ")
        print("   done — Hunch reuses this profile in the background from now on.")

    print("\n5) Default gate policy")
    if os.path.exists(policy.CONFIG_PATH):
        print(f"   {policy.CONFIG_PATH} already exists — leaving it as-is.")
    else:
        policy.save(policy.default_config())
        print(f"   wrote {policy.CONFIG_PATH} (all confirmation gates on).")

    print("\nSetup done. Next: `hunch doctor` to verify, then `hunch connect <host>`.")
    return 0


# ── main ───────────────────────────────────────────────────────────────────────


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hunch",
        description="Hunch — drive this Mac focus-free over MCP (OS APIs, AppleScript, web/CDP, Accessibility).")
    parser.add_argument("--version", action="version", version=f"hunch {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the MCP server on stdio (what your MCP host launches)")

    p = sub.add_parser("setup", help="one-time interactive setup: permissions + browser profile")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="check every layer; PASS/WARN/FAIL per line")
    p.add_argument("--notify", action="store_true", help="also fire a test desktop notification")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("connect", help="register Hunch in an MCP host's config")
    p.add_argument("host", choices=["claude-desktop", "claude-code", "cursor"])
    p.add_argument("--project", action="store_true",
                   help="cursor: write ./.cursor/mcp.json instead of the global config")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("config", help="show or change the confirmation-gate policy")
    csub = p.add_subparsers(dest="action")
    csub.add_parser("show", help="print the effective policy")
    ps = csub.add_parser("set", help="hunch config set gates.shell off")
    ps.add_argument("key")
    ps.add_argument("value", choices=["on", "off"])
    csub.add_parser("reset", help="restore defaults (all gates on)")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("creds", help="manage Keychain credentials agents can use but never see")
    csub = p.add_subparsers(dest="creds_action", required=True)
    pa = csub.add_parser("add", help="save a login (or --secret for an API key/token)")
    pa.add_argument("service", help="short name agents refer to, e.g. 'google'")
    pa.add_argument("--secret", action="store_true", help="a single protected value, not username+password")
    pa.add_argument("--domain", action="append",
                    help="domain(s) this credential may be filled on (repeatable)")
    csub.add_parser("list", help="list saved services (names/kinds/domains — never values)")
    pr = csub.add_parser("remove", help="delete a saved credential")
    pr.add_argument("service")
    p.set_defaults(func=cmd_creds)

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return sys.exit(cmd_serve(args))
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
