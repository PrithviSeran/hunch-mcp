# AGENTS.md — working on Hunch

Context for coding agents modifying this repo. (User-facing "how to use Hunch" lives in
`README.md`; this file is "how the code works and how not to break it.")

Hunch drives a real, logged-in Mac for an LLM **focus-free** — reading and acting on
background apps without stealing the user's screen, cursor, or keyboard. That invariant is
the product. Most gotchas below exist to protect it.

## Setup & tests

```bash
.venv/bin/python -m pytest tests/          # full suite; needs pyobjc (the venv has it)
.venv/bin/python -m pytest tests/test_smoke.py
```

- Python ≥ 3.11. Depends on **pyobjc** (AppKit/ApplicationServices/Quartz) — macOS only.
- `tests/test_smoke.py::test_tool_count` asserts **exactly 29 MCP tools**. Adding/removing a
  tool means updating that number in the same commit, on purpose.
- Tests fake all AX/Quartz calls (`monkeypatch`), so they run headless with no UI and touch
  no Keychain. Keep new logic unit-testable this way: put the OS call behind a function you
  can patch, assert on the decision logic.

## Architecture — one engine, three faces

The SDK is the product; everything else is an app built on it.

- **`sdk.py`** — `Hunch`, the developer-facing SDK object (instance-owned policy, auth
  injection `ApiKey`/`OAuthToken`/`"none"`, `app_id` namespacing, notify handler).
- **`server.py`** — the MCP server, **an app built ON the SDK** (the first one). 29
  `@mcp.tool()` functions each call `_run(name, ...)` → `agent._dispatch_core`. Its only
  "personal machine" specialness is constructor args (`policy="personal"`, Hunch branding).
- **`agent.py`** — the agent loop (`Agent.run`), two backends: **api** (`anthropic`, metered)
  and **subscription** (`claude-agent-sdk`, the user's Claude sign-in). `backend="auto"`
  picks by ambient credentials. **`_dispatch_core(mac, name, args)` is the single tool-execution
  engine** shared by both the server and the agent loop — expected Hunch exceptions become
  plain content strings so the model can adapt; only unexpected ones set `is_error`.
- **`local_mac.py`** — the macOS backend: `MacSession` (AX perception + action primitives)
  and `LocalComputer` (exposes snapshot/act/screenshot to the loop). The biggest, most
  delicate file.
- **`ax_tree_mac.py`** — low-level AX helpers (`get_attr`/`get_attrs`, `get_window`,
  `get_actions`, `values_to_bounds`, tree walk). One IPC round-trip matters here; huge trees
  (Mail inbox = one AXRow per email) make naive per-attribute reads take minutes.
- **`cdp.py`** — the web/Electron backend (Chrome DevTools Protocol), background-driven.
- **`gate.py` / `policy.py`** — consent. `Gate.front_gate` / `confirm_dialog`; `confirm="off"`
  or `HUNCH_NO_INTERNAL_GATE=1` env = auto-approve (host owns permissions, e.g. the desktop app).
- `os_ops.py` (files/clipboard/AppleScript), `creds.py`/`auth.py` (Keychain), `notify.py`,
  `errors.py`, `playbook.py` (`HUNCH_PLAYBOOK`; `server.py` serves it as `FastMCP(instructions=…)` so every connected client receives it. Change the model-facing contract here, not the tool docstrings.), `cli.py`.

### Adding or changing a tool

A tool touches **three** places — keep them in sync:
1. **`server.py`** — a `@mcp.tool()` function calling `_run("name", ...)`.
2. **`agent.py`** — an entry in `AGENT_TOOLS` (schema) and one in `_DISPATCH` (name → callable).
3. The implementation (usually a `MacSession`/`LocalComputer` method, or `os_ops`).

Then bump `test_tool_count`. Native-AX actions (click/type/window/…) are *not* separate tools;
they're `action` values inside the `act` tool — edit `_ACTION_ITEM` (agent.py) and the `TOOLS`
`act` schema (local_mac.py), which must agree.

## The focus-free contract (do not regress)

Hunch may **never** touch the shared cursor/keyboard or raise an app except as a gated last
resort. Concretely:

- **Prefer the AX layer**: `AXPress` / `AXUIElementSetAttributeValue` trigger elements with no
  cursor movement and no app activation. `snapshot`/`act` operate on background windows.
- **Shared-input fallbacks (pixel click, keystrokes) must `activate()`-or-refuse** — never post
  a CGEvent blind. See `click`/`right_click`/`set_text` in `local_mac.py`: if the AX path fails
  and `allow_pixel`/`allow_keystrokes` is off (simultaneous mode), they return a helpful refusal
  string, not an action.
- **`MacSession.disturbances`** counts every shared-input use (pixel clicks, keystrokes, key
  combos, app raises). `act()` appends a per-call receipt when a call disturbed the screen. If
  you add a shared-input path, increment the right counter — the receipt is how "focus-free" is
  audited in-band.
- Layer priority, most-direct first: **OS-API → AppleScript → Web/CDP → AX → vision**. Vision
  (`screenshot` + `click_xy`) is the gated last resort; don't reach for it when a tree read works.

## AX-layer knowledge (hard-won — read before touching `local_mac.py`)

- **The walk is serial by design — batching is the IPC win, concurrency is not** (measured,
  `bench/ax_traversal_bench.py` + `ax_bottleneck_probe.py` + `ax_multiapp_probe.py`). Each AX read
  is a synchronous Mach round-trip to the target app. `ax.get_attrs`
  (`AXUIElementCopyMultipleAttributeValues`) reads **all** of an element's attributes in **one**
  round-trip — that's the ~5–7x speedup and the whole game. Reading the tree across a **thread pool
  does NOT help** (benchmarked ~20–26% *slower*): the target app answers AX on its **single main
  thread**, so concurrent reads of one app's tree can't overlap. It is **not** the GIL — the pyobjc
  call releases it (bracket ratio 0.98) — the limit is server-side in the other process, so
  free-threaded Python won't change it. A concurrent BFS prefetch (`_prefetch`/`walk_workers`) was
  **removed in 0.5.1; do not re-add it.** (Concurrency only scales across *different* apps — separate
  main threads — never within one.)
- **Stable refs**: `snapshot` assigns `[eN]` per element via a keymap; the same element keeps its
  ref across snapshots. `act()` returns a **delta** (`~ changed / + new / gone:`) vs the last
  snapshot, full tree only on first view / window change / >50% churn. Don't assume `act` re-sends
  the whole tree.
- **The `_ax_activate` ladder** (`click` uses it): try the element's own `AXPress` → press-like
  alternative actions (`AXOpen`/`AXConfirm`/…) → a **read-back-verified** `AXValue` flip for
  checkbox/switch roles → then sweep **descendants** (ref often lands on a wrapper row) → then
  one **parent** (ref often lands on the *label*; the real control is a sibling).
- **SwiftUI apps lie** (System Settings, Shortcuts, and more each release):
  - `AXPress` returns **success on `AXStaticText`** while doing nothing — never trust an
    `AXPress` "success" on an inert role; verify the state actually changed.
  - SwiftUI switches **reject `AXPress`** but accept an `AXValue` write — which they then
    **silently drop**. Always read the value back before reporting success. (`_ax_fire`.)
  - The task-winning control is often a *sibling* of the ref you were given (label vs control).
- **Window move/resize**: use the `window` act action → `MacSession.set_window`, which targets
  `AXMainWindow` (via `get_window`), **not `window 1` by index**. A modal **sheet** (save panel,
  a locked-note password prompt) can *be* window 1; index-based resize hits the sheet. It reads
  the geometry back and reports the actual result (windows clamp/refuse).
- **Embedded Chromium/Electron** (Discord, Slack, VS Code, Spotify…) don't expose their web-content
  AX tree in the background. `launch_app(force_accessibility=True)` relaunches with
  `--force-renderer-accessibility` so the tree persists; `AXManualAccessibility` helps while
  frontmost. Detected via bundled `Electron Framework`/`Chromium Embedded Framework`.
- **When the AX tree is genuinely empty** (a SwiftUI *custom canvas* like the Shortcuts editor:
  ~5 nodes, all placeholder text), there is **no lower level to read** — `AXUIElement` is the
  floor of the public API and reads what the app *published*. `AXEnhancedUserInterface` /
  `AXManualAccessibility` are rejected by such apps (tested). The only recourse is vision, or a
  **data backdoor** (an app's scripting/file/API path — the native-first instinct). Reading the
  in-process SwiftUI view graph would need code injection, which SIP/hardened-runtime blocks for
  system apps.

## Frontmost detection — a real trap

**Never use `NSWorkspace.frontmostApplication()` to read the current front app in this codebase.**
It is KVO-cached and **never updates** in a process that isn't pumping an `NSRunLoop` — i.e. the
MCP server and every plain script. It silently returns a frozen value. `activate()`'s poll loop
believed every raise failed and `act()` then refused actions that had actually worked.

Use **`_frontmost()`** (`local_mac.py`) — a fresh `lsappinfo` query per call. This applies to
**all** pyobjc frontmost/observer polling from non-run-loop threads, not just this one call site.

## Vision path (`screenshot` + `click_xy` + `drag`)

- Requires **Screen Recording permission** for the *host* process (TCC attributes it to the
  terminal/app, not the child). Without it `screencapture` fails; the error string tells the user
  how to fix it and to prefer `snapshot`. In headless/CI, vision is simply unavailable.
- **Retina scaling is handled (do not undo it).** `screencapture` grabs the full screen at native
  resolution (2x on a Retina panel), but `click_xy` posts CGEvents in 1x points. `screenshot`
  DOWNSCALES the PNG to point space (via `sips -z`, using `backingScaleFactor`) so image coords
  equal `click_xy` coords. Remove that downscale and Retina misclicks return; they are intermittent
  because a 1x external display hides them.
- **There is a drag primitive.** `act`'s `drag` action (`_mouse_drag`: press, move with intermediate
  dragged events, release) covers canvas drag-and-drop, sliders, and reorder lists that ignore a
  bare down/up. Endpoints resolve from `{side}_ref` element centers or explicit points. It is a
  shared-input path like `click_xy`/`key`: gated, and it bumps `disturbances["drags"]`.

## Conventions

- Match the file's existing style; comments state *why/constraints*, not what the next line does.
- Expected failures return an explanatory **string the model can act on** (see the refusal
  messages) — don't raise for a situation the agent should adapt to. Reserve exceptions for
  genuinely unexpected states (`_dispatch_core` flags only those as `is_error`).
- Don't name a `threading.Thread` attribute `self._stop` — it shadows `Thread._stop` and `join()`
  raises `TypeError`.
- Env knobs are read at **call time**, not import, so they apply however late they're set
  (`HUNCH_NOTIFY_FOCUS`, `HUNCH_NO_INTERNAL_GATE`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`).
- `as_str` (AppleScript string-quoting) lives once in `notify.py` (stdlib-only); `gate` re-exports
  it. The Chrome CDP profile path lives once in `cli.py` (`CHROME_PROFILE`), imported by `cdp.py`.

## Packaging, release & distribution (learned the hard way this cycle)

- **PyPI package is `hunch-sdk`; the command is `hunch`.** `[project.scripts]` maps
  `hunch = "hunch.cli:main"`. The MCP server ships in the BASE package (`hunch serve`), not behind
  an extra, and `import hunch` gives the SDK object. One package, both faces.
- **The sdist is allowlisted on purpose; do not remove it.** `[tool.hatch.build.targets.sdist]
  include = ["src/hunch", "README.md", "LICENSE"]`. Hatchling's default sdist sweeps the whole
  project dir, and `bench/` (benchmark transcripts, hundreds of MB) is ignored only via
  `.git/info/exclude`, which Hatchling does NOT read. Without the allowlist the sdist ballooned to
  368 MB (now ~184 KB). `bench/` also will not appear in `git status`, for the same reason.
- **The `mcp-name` marker must stay in `README.md`.** The MCP registry verifies ownership by finding
  `mcp-name: io.github.PrithviSeran/hunch` in the PyPI DESCRIPTION, which is the shipped `README.md`
  (kept as an HTML comment at the top). The namespace must match the GitHub account casing exactly
  (capital P), or publishing 403s.
- **`server.json`** (repo root) is the MCP-registry manifest; **`mcp.json`** (repo root) is the Open
  Plugins / Cursor-directory manifest. Bump `server.json`'s `version` alongside `pyproject.toml`.
- **PyPI publishing is automated.** `.github/workflows/publish.yml` fires on merge to `main` when
  `src/hunch/**` or `pyproject.toml` changed, and publishes **only if** the `pyproject.toml` version
  isn't already on PyPI (a merge without a version bump is a safe no-op). It builds (`python -m
  build`), uploads via `pypa/gh-action-pypi-publish` over OIDC **Trusted Publishing** (no API token),
  and tags `vX.Y.Z` + cuts a GitHub release. So a release is just: bump `pyproject.toml` version,
  merge to `main`. Requires the one-time PyPI trusted-publisher config (project `hunch-sdk`, owner
  `PrithviSeran`, repo `hunch-mcp`, workflow `publish.yml`, environment `pypi`).
- **Publishing order:** merge the version bump so the workflow ships PyPI, then
  `mcp-publisher login github && mcp-publisher publish`. Footguns: `mcp-publisher publish --dry-run`
  actually PUBLISHES in the current CLI, and re-publishing the same version 400s ("duplicate
  version"), so a registry update needs a real version bump.
- **Homebrew is a separate repo** (`PrithviSeran/homebrew-hunch`, `Formula/hunch.rb`). It just
  `pip install`s the sdist into a venv (no `resource` blocks), so a release bump is only `url` +
  `sha256`. It does NOT auto-follow PyPI; bump it every release or `brew` users get a stale build.
- **The website is `site/`** (self-contained static site; deploy on Vercel with root directory
  `site`). Blog pages are generated: `site/build.py` renders `site/content/*.md` (front matter +
  Markdown, charts embedded as raw HTML). Edit the `.md`, run `python3 site/build.py`, commit. The
  raw benchmark harness/data live in the git-excluded `bench/`, so the reproducible benchmark is the
  separate `mac-agent-bench` repo, not this one.
