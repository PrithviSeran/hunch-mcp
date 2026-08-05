# Postmortem — the session in TRANSCRIPT.md

**Task:** open `~/hunch` in a Cursor window and start Claude Code in its terminal.
**Cost:** ~48 tool calls, 4 user interventions, one rejected tool. The task itself is 2 calls.
**Cause:** not a missing capability. Every layer Hunch needed already worked. Six separate places
reported success for something that hadn't happened, and the agent believed them — correctly, since
nothing in the returned data contradicted them.

## The one sentence version

An agent can only ever see *a* tree, and a tree of the wrong window looks exactly like a tree of the
right one. Hunch handed back a valid tree of someone else's project, labelled with the folder that
had been asked for, and had no way to say otherwise.

## What actually happened

`web_open(app="Cursor", url="/Users/prithviseran/hunch")` returned:

> opened Cursor on /Users/prithviseran/hunch over CDP (327 elements), focus-free

The session was on `C63`, an unrelated course folder. Two defects produce this, and the transcript
can't distinguish which fired first — both are real and both are fixed:

1. `launch_chromium` **reuses a live instance and drops the `url` argument**. The browser path masks
   this by calling `navigate()` afterwards; the editor path had no equivalent, so the folder was
   never delivered to anything.
2. `_pick_workbench` chose the **first** workbench target. An editor holds several windows (it
   restores the previous session's on launch) and they all share one `workbench.html` url, so the
   pick was a coin flip that ignored the requested folder entirely.

The next ~20 calls were the agent trying to reconcile a message it trusted with a tree that
disagreed. Every diagnostic it reached for was also broken:

3. `applescript` "count of windows" returned **empty, three times**, because System Events reports 0
   windows for any Electron app. No error, no explanation — so retrying the same script was the
   rational next move.
4. `snapshot(app="Cursor")` read a *different Cursor process* than CDP was driving (Hunch launches
   its own copy on a dedicated profile; `_resolve_app` picks one by name). Both returned valid trees.
   Nothing said they were different windows.
5. Driving the native **Open panel**: `select` on a file row returned `"selected e121"` while the row
   stayed unselected (the panel tracks selection on the parent's `AXSelectedRows`), so the Open
   button never enabled and nothing explained the contradiction. `click` on the same row fired
   `AXShowDefaultUI`, which only toggles a disclosure triangle, and reported `"performed
   AXShowDefaultUI"` — indistinguishable from a press.
6. The **playbook never says "don't drive an Open panel"**, and never mentions that `open_file(path,
   app="Cursor")` does this in one focus-free call.

The recovery the model eventually found on its own — drive the *bound* window's Command Palette into
`File: Open Recent` — is the correct move and appears nowhere in the SDK.

## Fixed in this branch

| Defect | Fix |
| --- | --- |
| Folder dropped on instance reuse | `_open_editor` verifies with `bind_workspace`, then hands the folder to the live instance via `open_folder_in_editor` (second `open -na` on the same `--user-data-dir`; Electron's singleton forwards the argv) and retries |
| Wrong window picked | `_pick_workbench(pages, folder)` prefers the window whose **title** names the folder; `_title_matches` splits on em/en dash only, so `my-project` never matches `my` |
| Success claimed for an unreached workspace | Refuses: names the workspace it *is* on, lists the open windows by index, gives both recoveries (`web_switch_tab`, or Open Recent from inside the bound window) |
| Editor session silently following new targets | `_follow_new_tab` is a no-op for editors except when their own window dies |
| Two processes, one name | `snapshot` prepends a `[!]` line naming which copy it read and which one `web_*` drives |
| AppleScript empty result | Explains the Electron/System Events zero-windows rule and points at `snapshot`/`web_tabs` |
| `select` phantom success | Reads `AXSelected` back, tries the container's `AXSelectedRows`, else says the write was accepted and dropped — and steers to `open_file` |
| `AXShowDefaultUI` as a press | Never offered for row-ish roles; elsewhere it says plainly that it is not a press |
| Missing path | `web_open` on an editor checks the path before launching anything (a typo cost a launch + ~40 s of polling) |
| Playbook | New rules: never drive an Open/Save panel, editors are multi-window, Hunch's app copy ≠ the user's |

`web_tabs` also now lists editor **windows by workspace** instead of a truncated `vscode-file://`
url that was identical for every window.

Tests: 158 pass. `test_smoke.py::test_screen_approval_dedupe` fails, but it fails identically on
`main` — environment-dependent, unrelated to these changes.

## Not fixed — worth deciding on

**1. The product mismatch that started it.** The user said "open ~/hunch in a cursor window" and
meant *their* Cursor. Hunch's editor path deliberately launches a **separate, dedicated instance** on
its own profile. That's the right non-destructive default for background work, but here it produced
a window the user couldn't see while they insisted "cursor is literally the most frontmost window
right now" — they were looking at a different process. Options: teach `web_open` to say which window
the user should look at (it's a background window, so they may need to be told it exists), or add an
opt-in that attaches to the user's own editor (only possible if it was launched with a debug port —
which would need a `hunch` CLI command to relaunch it, and is destructive).

**2. Grind detection.** `web_open` was called 4× with identical arguments; the same AppleScript ran
3×. Nothing noticed. A repeated identical call that produced no state change is the strongest
available signal that the agent is stuck, and the SDK is the only layer that can see it. Returning
an escalating message on the 2nd or 3rd identical failing call would have cut this session in half —
it needs per-session call history, which no tool currently keeps.

**3. `key` actions are unverifiable and report success anyway.** `press_key` posts the event and
returns `"key Return"` whether or not anything received it — the transcript has four of those against
a modal sheet that never moved. Activation is checked once per `act()` batch, not per keystroke.
At minimum a `key` result should say which app was frontmost when the event was posted.

**4. No window enumeration at the AX layer.** `snapshot(app)` reads the *focused* window and there is
no way to list an app's windows or target a specific one. `kAXWindowsAttribute` gives this for free
and would have answered the agent's actual question in one call. It's a new tool (tool count 29 → 30,
`test_tool_count`), so it wants a deliberate decision rather than being folded in here.

## The rule worth generalizing

The best error message in the whole transcript already exists in this codebase — `set_text` refusing
to write into an xterm.js terminal:

> `e471` is a terminal inside Cursor — AX cannot write to it. xterm.js reads keystrokes, not AX
> values, so an AX set would silently do nothing… Type into it FOCUS-FREE over CDP instead: …

It names the failure, explains the mechanism, and hands over the exact working alternative. The agent
followed it immediately and never revisited it. Every fix above is written to that standard, because
the failure mode here was never a missing capability — it was six confident sentences that weren't
true.
