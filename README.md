<!-- mcp-name: io.github.PrithviSeran/hunch -->
<p align="center">
  <img src="https://raw.githubusercontent.com/PrithviSeran/hunch-mcp/main/src/hunch/assets/hunch.png" alt="Hunch" width="128">
</p>

# Hunch

**Drive your Mac with any LLM: focus-free, in the background, over [MCP](https://modelcontextprotocol.io).**

Hunch is an MCP server that gives an LLM agent hands on *your* Mac: your installed apps, your
logged-in sessions, your files, without taking over your screen. While you keep working in the
foreground, an agent can read a background app's UI, click its buttons, drive Mail or Music by
AppleScript, fill a web form, or move files. It works on real native apps, not just a browser.

> **Works best with modern LLMs.** Hunch ships a detailed playbook as MCP server instructions;
> capable tool-using models (Claude Sonnet/Opus-class and up) follow it well. Smaller models may
> pick clumsier paths (screenshots and keystrokes instead of tree reads and clicks).

## The four layers

Hunch always prefers the most direct layer. It's faster, more reliable, and (except the last)
never touches your screen:

| Layer | Tools | What it's for |
|---|---|---|
| **OS-API** | `trash` `file_op` `open_file` `clipboard_*` `launch_app` … | files, clipboard, app lifecycle, via direct API calls |
| **AppleScript** | `applescript` | scriptable apps: Mail, Messages, Notes, Calendar, Music, Finder, Safari … |
| **Web / CDP** | `web_open` `web_snapshot` `web_act` `web_login` … | any browser page or Electron app, driven in the background |
| **Accessibility** | `snapshot` `act` | any native app's UI: read the tree, click/select/type by reference |

A gated last resort (`screenshot` + coordinate clicks/keystrokes) exists for apps whose
accessibility tree is truly empty. It steals focus, so it asks you first.

## How it works (no server, no cloud)

"MCP server" undersells how local this is. `hunch serve` is a plain Python process that your
MCP host (Claude Desktop, Cursor, …) spawns as a **child process** and talks to over
**JSON-RPC on stdin/stdout** (MCP's stdio transport). There is no HTTP endpoint, no port
Hunch listens on, no daemon, and no telemetry. When your host quits, Hunch is gone.

The tools are direct macOS API calls in-process: the Accessibility framework via pyobjc,
`osascript` for AppleScript, OS APIs for files/clipboard, and, for the web layer, a local
WebSocket to Chrome's DevTools port on `127.0.0.1`. The only thing that ever touches the
network is Chrome itself, doing ordinary browsing. What the model sees is whatever the tools
return through your host; nothing else leaves the machine.

## Benchmarks

Hunch is measured against other macOS computer-use agents on a real, logged-in Mac in
[mac-agent-bench](https://github.com/PrithviSeran/mac-agent-bench) — *same brain, different hands*:
identical `claude -p` per task, only the MCP adapter differs. It scores task success with programmatic
checkers **and** disturbance: how much the agent hijacks your cursor and foreground while it works.

Across 5 complex multi-step tasks (n=3):

| Tool | Success | Cost | Disturbance (focus·cursor) | Timeouts |
|---|---|---|---|---|
| **Hunch** | **15/15** | **$1.52** | **1·0** | **0** |
| [Peekaboo](https://github.com/steipete/Peekaboo) | 13/15 | $14.60 | 22·16 | 2 |
| [cua-driver](https://github.com/trycua/cua) | 15/15 | $17.70 | 22·0 | 0 |

Perfect reliability, ~10x cheaper, ~5–10x faster, and it essentially never touches your screen (`0`
cursor moves, `1` focus switch across 15 trials). Full methodology and per-task tables are in the
[benchmark repo](https://github.com/PrithviSeran/mac-agent-bench).

## Install

macOS 13+. From PyPI (the distribution is `hunch-sdk`; the import and CLI are `hunch`):

```
pipx install hunch-sdk          # or: pip install hunch-sdk
pip install 'hunch-sdk[subscription]'    # + the agent loop on Claude   (provider="claude")
pip install --pre 'hunch-sdk[codex]'     # + the agent loop on OpenAI Codex (provider="codex")
```

Or via Homebrew — best if you don't manage Python environments; it bundles an isolated
Python at a stable path, which makes the macOS permission grants the most predictable:

```
brew install prithviseran/hunch/hunch
```

Then, one time:

```
hunch setup      # walk the macOS permission grants
hunch doctor     # verify every layer; fix anything it flags
hunch connect claude-desktop   # or: claude-code, cursor
```

Restart your MCP host and ask it to *"use hunch to …"*.

### The permissions, honestly

macOS trust attaches to the **app that runs the server**, meaning your MCP host (Claude Desktop,
Cursor, your terminal), not "hunch" itself. `hunch setup` walks you through it:

- **Accessibility** (required): lets Hunch read app UIs and click focus-free. Grant it to your MCP
  host app in System Settings → Privacy & Security → Accessibility.
- **Automation** (per-app, automatic): the first time Hunch scripts an app, macOS shows a one-time
  "allow control" prompt.
- **Screen Recording** (optional): only for the screenshot/vision fallback.

`hunch doctor` reports what's granted. Note: its Accessibility line reflects the *terminal* you ran
it from; the server inherits the *host's* grant.

## Python SDK (library use)

Hunch is also an importable library — the same focus-free primitives as the MCP tools, driven
deterministically from your own Python (a cron job, a test harness, your own agent loop), no LLM
required. The distribution is `hunch-sdk`; the import is `hunch`:

```python
from hunch import Hunch

mac = Hunch()                              # your machine, your logged-in apps
print(mac.snapshot("Mail"))                # accessibility tree, focus-free
mac.act([{"action": "click", "ref": "e12"}])
mac.web.open(url="https://github.com")     # real persistent Chrome profile over CDP
print(mac.web.snapshot())
mac.files.trash(["~/Downloads/old.zip"])   # reversible delete, no Finder
mac.applescript('tell application "Music" to play')
```

Constructor knobs: `app` (initial snapshot target), `confirm="dialog"|"off"` (see below),
`check_permissions` (Accessibility check up front), `simultaneous` (never touch the
foreground/cursor/keyboard), `cdp_port`.

- **Permissions**: for library use it's *whatever runs your script* — your terminal or IDE — that
  needs Accessibility (the MCP server instead uses the host app's grant). The constructor checks
  and raises `AccessibilityNotGranted` with instructions. `screenshot()` additionally needs
  Screen Recording.
- **Safety gates default ON**: the same one-click "Go ahead" dialogs and `~/.hunch/config.json`
  gates as the MCP server. `Hunch(confirm="off")` auto-approves for that instance only — for
  unattended scripts, with the same caveats as `auto_approve_all`.
- **Errors**: methods return status strings (check for `REFUSED`); the SDK raises only
  `ApprovalDenied` (user declined a dialog), `AccessibilityNotGranted`, `WebNotOpen`
  (`.web` before `.web.open()`), `StaleRef` (re-snapshot), and `HunchError` when a CDP
  browser can't be opened (`web.restart()` recovers a stale instance).
- **Credentials**: `mac.web.fill_login(service)` / `fill_secret(service, ref)` type Keychain
  values straight into the page and never return them; domain binding is enforced.
- **Coexistence**: the SDK and the MCP server share the CDP port (9337) and the persistent Hunch
  browser profile — whichever opened it first is reused, but `web.restart()`/`web.login()` kill
  whatever holds the port.

Runnable scripts live in [`examples/`](examples/).

## Agent loop (`mac.agent`)

The instance SDK gives you deterministic primitives. The **agent loop** puts an LLM in the driver's seat:
you hand it a task in plain English and it drives the Mac through those same primitives — on *your*
machine with *your* logged-in apps. Pick your **provider** — Anthropic **Claude** or OpenAI **Codex** —
once at construction; everything after is provider-agnostic. It's an optional extra (keeps the base
install free of the model SDKs):

```bash
pip install 'hunch-sdk[subscription]'                    # Claude
# or, for Codex (the SDK is beta):  pip install --pre 'hunch-sdk[codex]'
python -c 'import hunch; hunch.provider("claude").login()'   # sign-in (browser OAuth) — no API key
```

```python
from hunch import Hunch

mac = Hunch(provider="claude")          # or Hunch(provider="codex")
result = mac.agent.run("reply to Sarah's latest email, but don't send it")
print(result.text)          # the model's final summary
print(result.turns, result.usage)
```

### Signing in

Auth is an explicit, provider-scoped surface — nothing is scavenged silently. You choose the vendor
once, then the same prefix-free methods act on it:

```python
mac = Hunch(provider="codex")       # or "claude" (the default)
mac.login()                         # codex: ChatGPT browser sign-in · claude: Claude sign-in
mac.status()                        # -> AuthStatus(provider, logged_in, method, email, plan)
mac.logout()
mac.agent.run(task)                 # runs on the configured provider
```

Or drive auth standalone, without a Mac instance (e.g. an onboarding script):

```python
import hunch

st = hunch.provider("codex").status()
if not st.logged_in:
    hunch.provider("codex").login()               # browser OAuth (blocks until done)
    # headless/remote box instead:
    #   h = hunch.provider("codex").device_login()
    #   print(h.verification_url, h.user_code); h.wait()
```

| Provider | Sign in with | Cost |
|---|---|---|
| `claude` | `mac.login()` / `hunch.provider("claude").login()` — the same browser OAuth Claude Code uses. Already signed into Claude Code on this Mac? You're done; Hunch reuses that. | your Claude plan (no per-token cost) |
| `codex` | `mac.login()` / `codex login` — a ChatGPT/Codex browser sign-in. | your ChatGPT/Codex plan |

Both run on a subscription/login — metered API keys are intentionally not exposed. With no valid
sign-in, `run()` surfaces an error that points you back to `login()` — it never guesses.

- **Watch it work** with an `on_event(kind, data)` callback — `kind` is one of `text` (reasoning),
  `tool` (`{name, input}`), `tool_result` (preview), `done` (final text), `error`.
- **Continuation**: follow-up `run()` calls keep the conversation (the model still knows which email
  is Sarah's); `mac.agent.reset()` starts a fresh task.
- **Mix layers freely**: call `mac.snapshot(...)` / `mac.clipboard.get()` deterministically around
  `mac.agent.run(...)` — the thing a cloud sandbox can't do on your real machine.
- **Knobs**: `run(task, model=None, max_turns=40, effort=None, on_event=None, system_suffix="")` —
  `model=None` uses the provider's own default model. `AgentResult` has `text`, `turns`,
  `stop_reason`, `usage`, `aborted`.
- **Safety**: the instance's gate config governs the loop. The default `confirm="dialog"` pops a
  real "Go ahead?" dialog before any focus-stealing or risky step — good when you're at the
  machine, but a gated action can stall an unattended run for the dialog's timeout. For cron jobs
  use `Hunch(confirm="off")` and accept the risk; the model still asks *you* (via `notify_user`)
  before irreversible or outward actions. A declined gate comes back as a `REFUSED` result, so the
  loop adapts instead of crashing.
- **Cost**: runs draw on your provider plan's usage limits — no per-token bill. `max_turns` caps it.

> **Codex note:** the `codex` provider drives an OpenAI Codex agent that reaches Hunch's tools over
> MCP (a separate `hunch serve` process), so its dangerous-verb approvals use Hunch's own
> click-to-approve dialogs rather than a host `can_use_tool` callback.

Other models: the instance-SDK primitives are provider-agnostic — wire `mac.snapshot()` /
`mac.act()` into your own OpenAI/Gemini/etc. agent loop as tools.

## Building an app on Hunch

The SDK is developer-first: **one uniform semantics, everything instance-owned, nothing ambient
unless you opt in.** The MCP server above is itself just the first app built on it — its
"personal" behavior (the `~/.hunch/config.json` policy, "Hunch"-branded dialogs, shared browser
profile) is nothing but constructor arguments.

```python
from hunch import Hunch, ConsentRequest, OAuthToken

mac = Hunch(
    provider="claude",                  # which LLM vendor drives mac.agent ("claude" | "codex")
    app_id="com.acme.mailbot",          # pure namespacing — own Keychain slots, browser
                                        #   profile, and CDP port; never a behavior switch
    app_name="Acme Mailbot",            # what consent dialogs + notifications say
    confirm=my_consent_callback,        # ConsentRequest -> bool, rendered in YOUR UI
    notify=my_toast_handler,            # (message, title) -> your surface, not macOS banners
    policy={"gates": {"shell": True}},  # instance-owned safety; the user's personal
                                        #   config can never disarm your app
    auth=OAuthToken(token),             # the exact Claude subscription token YOUR app manages
    can_use_tool=my_approver,           # optional: route every tool through your Approve/Deny UI
)
```

What this buys you:

- **Coexistence** — two apps with different `app_id`s get disjoint Keychain services, credential
  stores, browser profiles, and CDP ports. They can't read each other's logins, log each other
  out, or kill each other's browser sessions. Structurally, not by convention.
- **Your brand, your UX** — every dialog, refusal, and notification says your `app_name`;
  `confirm=` and `notify=` route consent and alerts through your app instead of osascript
  dialogs and macOS banners. A broken consent callback fails **closed**.
- **Isolated safety posture** — the machine's `hunch config` (and `HUNCH_NO_INTERNAL_GATE`)
  govern only the personal MCP server, never your instance.
- **Explicit auth** — `provider="claude"` with `auth=hunch.OAuthToken(...)` uses exactly that
  subscription token (reprs are redacted), so your app never silently rides on the end user's own
  Claude sign-in. (The `codex` provider authenticates via `mac.login()` / `codex login`.)

Programmatic credential management uses the same namespacing: `hunch.creds.set_credential(name,
user, pw, namespace=your_app_id)` etc. A runnable walkthrough lives in
[`examples/embedded_app.py`](examples/embedded_app.py).

## Credentials: agents use them, never see them

```
hunch creds add github --domain github.com
hunch creds list
```

Values go straight into the **macOS Keychain**. An agent signs in by calling
`web_fill_login("github")` with only the service *name*; Hunch reads the secret from the Keychain
and types it into the page over CDP. The value never enters the model's context, its logs, or its
provider's servers.

**Domain binding**: a credential added with `--domain github.com` will only ever be typed into
`github.com` (and its subdomains). If a confused or prompt-injected agent lands on a look-alike
page, the fill is refused. Bind every credential; blank (any-site) exists only for compatibility.

No stored credential? Agents fall back to `web_login`, which opens a tagged browser window where
*you* sign in yourself; the session then persists in Hunch's dedicated browser profile.

## Confirmation gates

Your MCP host's tool approvals are the primary permission layer. Hunch adds a content-aware second
gate, a one-click macOS dialog, for the catastrophic cases:

| Gate | Fires on |
|---|---|
| `gates.focus_steal` | actions that take over your keyboard/cursor (`key`, `click_xy`, ref-less typing) |
| `gates.app_to_front` | an app being brought to the front (a focus switch, even mid-fullscreen) |
| `gates.shell` | AppleScript containing `do shell script` |
| `gates.destructive_applescript` | delete / send / empty trash / shut down / … |

One approval covers its follow-through: clicking "Go ahead" (on `request_focus`, a gated `act`,
or the app-to-front dialog) authorizes the switch it announced for ~15 s: no second dialog, and
the focus-switch notification is suppressed. A switch is either *asked about* or *announced*,
never both, and never silent (turn `gates.app_to_front` off and switches fall back to the
notification).

All on by default. For the **MCP server**, `hunch config show` / `hunch config set gates.shell off`
adjusts them; changes apply immediately, even to a running server. `auto_approve_all` disables
everything and makes you confirm you understand the [risk](SECURITY.md).

For **SDK instances** the gates are instance-owned: `Hunch()` defaults to all gates on and never
reads the config file — pass `policy={"gates": {...}}`, a `callable(category) -> bool`, or
`policy="personal"` to opt into the live config-file behavior for your own scripts.

Hunch also refuses to let the agent edit anything under `~/.hunch/` (its own policy and credential
metadata) via its file tools; permission changes are for humans in a terminal.

## Env vars

| Var | Effect |
|---|---|
| `HUNCH_NO_INTERNAL_GATE=1` | suppress all internal dialogs (for host apps that run their own approval UX) |
| `HUNCH_FORCE_SANDBOX=1` | web layer uses a throwaway, logged-out browser profile |
| `HUNCH_NOTIFY_FOCUS=0` | silence the "Hunch is switching apps" notifications (they fire only for switches no dialog asked about) |

## FAQ

**Every tree read returns "(no window for …)" and AppleScript fails with "-25211 not allowed
assistive access".** One cause: the app hosting Hunch is missing the **Accessibility** grant.
Without it the AX API silently returns nothing, so apps look windowless even when they're open.
Grant it to the *host* (see next question), and if it's already listed, toggle it off and on:
macOS silently invalidates grants when an app updates. Restart the host afterwards.

**Which app do I grant permissions to?** The one that *launches* `hunch serve`: Claude Desktop,
Cursor, or your terminal app (for Claude Code). Grants attach to that app's identity, never to
"hunch" itself. This is also why `hunch doctor` can be misleading: it reports the grants of the
terminal you ran it in, which may differ from your MCP host's.

**Only `screenshot` fails; everything else works.** That's the **Screen Recording** permission,
which only the screenshot/vision fallback needs. Grant it to the host in System Settings →
Privacy & Security → Screen Recording, or just let agents use `snapshot`, which doesn't need it.

**It worked yesterday and broke today.** An update to your host app (or macOS) likely reset its
permission grants. Toggle the host off and on under Accessibility (and Screen Recording, if you
use it), then restart the host.

**An app's tree reads empty or shows only a sidebar.** Two different situations. Electron/CEF
apps (Discord, Slack, Spotify, VS Code) need an accessibility flag; `snapshot` relaunches them
once, in the background, to set it. Master-detail and Catalyst apps (WhatsApp, Mail) expose only
the pane you're in: the agent should click into an item by ref and re-snapshot; the detail pane
then appears. Also check the app actually has a window open.

**The web layer won't connect.** Chrome 136+ blocks the CDP debug port on your default profile, so
Hunch drives its own Chrome (a separate data dir at `~/.hunch/chrome-cdp`), not your everyday one.
Sign that profile into your Google account once (via `hunch setup` or `web_login`) and Chrome Sync
brings your logins with it; from then on it's already signed in.

**My host shows the server instructions truncated.** Cosmetic: some host UIs shorten the playbook
in their server-info display; the model receives it in full.

## Security

Read [SECURITY.md](SECURITY.md): threat model (prompt injection, mainly), what the gates do and
don't cover, and how to report vulnerabilities.

## License

Apache-2.0; see [LICENSE](LICENSE).
