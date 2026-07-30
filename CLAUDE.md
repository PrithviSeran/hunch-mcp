# CLAUDE.md

See **[AGENTS.md](./AGENTS.md)** for how this codebase works and the gotchas that matter
(the focus-free contract, AX-layer traps, the `NSWorkspace` frontmost trap, adding tools,
the vision path, plus packaging/release). It's the canonical guide for coding agents; this file just points to it so
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
- **Releasing?** The sdist is allowlisted (`[tool.hatch.build.targets.sdist]`) so it does not sweep
  the git-excluded `bench/` (that once made a 368 MB sdist). PyPI package is `hunch-sdk`; on each
  release also bump `server.json`'s version and the separate `homebrew-hunch` formula. See
  AGENTS.md → Packaging.
- The model-facing contract is `playbook.py` (`HUNCH_PLAYBOOK`), served as the MCP server's
  `instructions`. To change agent behavior, edit it there, not the tool docstrings.
