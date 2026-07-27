# CLAUDE.md

See **[AGENTS.md](./AGENTS.md)** for how this codebase works and the gotchas that matter
(the focus-free contract, AX-layer traps, the `NSWorkspace` frontmost trap, adding tools,
the vision path). It's the canonical guide for coding agents; this file just points to it so
Claude Code loads it.

Quick reminders:
- Run tests: `.venv/bin/python -m pytest tests/`
- Adding/removing a tool changes the count in `tests/test_smoke.py::test_tool_count` (now 29).
- Never read the frontmost app with `NSWorkspace.frontmostApplication()` — use `_frontmost()`.
- Keep Hunch **focus-free**: shared-input fallbacks must `activate()`-or-refuse and bump
  `MacSession.disturbances`.
- The AX tree walk is **serial by design** — batching (`get_attrs`) is the IPC win; reading across
  threads doesn't help (the target app answers AX on one main thread). Don't re-add a concurrent
  prefetch (removed in 0.5.1). See AGENTS.md → AX-layer knowledge.
