---
title: "Do macOS agents hijack your screen? Benchmarking focus-free computer use"
description: "How we benchmarked three macOS computer-use agents on a real, logged-in Mac, measuring a dimension every public benchmark ignores: whether the agent steals your cursor and foreground while it works."
date: "{{PUBLISH_DATE}}"
lastUpdated: "{{PUBLISH_DATE}}"
author: "{{AUTHOR}}"
tags: ["computer-use-agents", "mcp", "macos", "benchmark", "accessibility-api"]
---

<!-- Data: n=5 confirmatory sweep, 2026-07-27, Claude Sonnet, one Apple Silicon Mac.
     hunch-main @ 8eed22a vs cua-driver 0.12.6 vs peekaboo 3.9.8. Full per-task table
     and caveats in bench/RESULTS.md. PUBLISH_DATE/AUTHOR still to fill at publish time. -->

There is a whole class of "computer-use agent" now: give a model a screenshot and a mouse, and it clicks around your machine to get things done. The benchmarks that score these agents all measure the same thing (did the task succeed?), and they almost all run inside a VM.

That leaves out the question we actually care about on a real, logged-in Mac: **while the agent worked, did it take over your screen?** Did it yank the cursor out from under you, raise apps to the foreground, and make the machine unusable until it finished? A VM has no user sitting at it, so no VM benchmark can even ask.

We built a benchmark that does. It runs 3 agents on a real Mac, scores task success with programmatic checks, and measures the thing we hadn't seen anywhere else: **disturbance**, meaning how many times the foreground app changed and how far the cursor moved while the agent was running.

## The metric the field ignores: disturbance

As of mid-2026 there are three credible macOS-native agent benchmarks: macOSWorld (NeurIPS 2025), MacArena (2026), and MacAgentBench (2026), plus OSWorld as the cross-OS standard. They are real, peer-reviewed work. They also share one structural blind spot: **every one runs the task inside a macOS VM** (AWS AMI, a local UTM image, or Docker-QEMU), and assumes a screenshot-driven agent that owns the foreground.

That design can't score the thing that decides whether you'd actually let an agent run on your own machine: whether it lets you keep working. So we defined a metric for it.

**Disturbance = focus switches + cursor travel, measured on the host while the agent runs.** A tool that drives your Mac through background APIs and the accessibility tree can complete a task with **zero** focus switches and **zero** cursor movement. You wouldn't know it ran. A tool that drives by moving the visible mouse and clicking racks up both. It is a direct, quantitative proxy for "did this hijack my machine."

<!-- [UNIQUE INSIGHT] the disturbance metric: no public macOS benchmark tracks host focus/cursor disturbance because they all run in VMs -->

## Same brain, different hands

The core fairness principle: hold the intelligence constant, vary only the tool.

Every trial is an identical `claude -p "<task prompt>"` running Claude Sonnet. The *only* thing that changes between runs is which MCP server the model is allowed to call: the "hands." We tested 3 sets of hands:

- **Hunch** (main @ 8eed22a): our tool. Drives native apps through OS APIs, AppleScript, the accessibility tree, and Chrome DevTools Protocol, all focus-free.
- **cua-driver** (trycua cua-driver 0.12.6): trycua's open-source driver. Markets focus-free host control via Apple's SkyLight window layer, exposed as primitive MCP tools.
- **Peekaboo** (@steipete/peekaboo 3.9.8): a screenshot-and-click vision loop.

Rules that make it a measurement and not a demo:

- **Locked hands.** Each run is `--strict-mcp-config` with every built-in tool denied except the MCP server under test (plus `ToolSearch`, needed to load deferred tool schemas). We verified isolation held by checking the transcripts: the model's occasional attempts to reach for `Read`/`Grep` were all denied. No tool got an escape hatch.
- **Real outcomes, no LLM judge.** Each task has a shell/AppleScript checker that inspects actual state: the draft exists in Mail, the reminder exists in Reminders, the two windows are actually tiled. A model grading itself is not evidence.
- **Hands-off, under `caffeinate`.** 5 trials per task. We left the machine alone; any cursor movement is attributable to the tool.
- **Cost from the source.** We read `total_cost_usd` from each run's result event, the CLI's own accounting.

## Building the instrument was harder than the benchmark

This is the part worth reading if you ever measure the same thing, because the naive approach silently produces all zeros.

**The frozen-frontmost trap.** The obvious way to read the current foreground app in a pyobjc process is `NSWorkspace.frontmostApplication()`. It is **KVO-cached and never updates in a process that isn't pumping an `NSRunLoop`** (i.e. any plain script or MCP server). It returns a frozen value forever. Our first monitor used it, reported `0` focus switches for everything, and looked great, until we watched a vision agent visibly drag files across the screen with the cursor while the monitor insisted nothing had happened. The fix: poll `lsappinfo front` (a fresh LaunchServices query) at 5 Hz, and track the cursor with a fresh Quartz `CGEventGetLocation` each sample. *Every focus number produced before we caught this measured nothing.*

<!-- [PERSONAL EXPERIENCE] caught the NSWorkspace bug because a vision agent visibly dragged files while the monitor showed 0 switches -->

**Retina misclicks.** `screencapture` grabs the screen at native resolution (2x pixels on a Retina panel), but the click API posts events in points (1x). Nothing conveys the backing-scale factor to the model, so vision clicks land at half-coordinates. It's intermittent because it disappears on a 1x external display. Worth knowing before you trust any vision-path result.

**SwiftUI lies.** System Settings and similar apps return `AXPress` *success* on inert static text while doing nothing, and their toggles accept an accessibility value write and then silently drop it. If you don't read the state back after acting, you record successes that never happened.

**Windows you can't enumerate.** For apps launched in the background, `System Events` (and even a direct `AXWindows` query) sometimes reports **zero windows**, while the app's own AppleScript reports one, a Spaces/enumeration quirk. Our window-tiling checker's first version depended on the fragile path and produced false failures for every tool. We rewrote it to read each app's own window geometry, which is robust.

The theme: on a real machine, the measurement apparatus lies to you in more ways than the agents do. Getting the instrument honest took longer than running the sweep.

## The tasks

11 scored tasks, spanning the layers a real assistant hits: native-API tasks (sort files, create a calendar event, save a Mail draft, copy a value to the clipboard), a GUI-only accessibility setting, window tiling, an Electron/CEF app (play a track in Spotify), two cross-app chains (read a file into a note; clipboard into a reminder), and a web task via CDP. Each has a deterministic checker and a setup/teardown that resets state between trials.

We excluded two by design (one has no accessibility tree at all, a genuine frontier case both tools fail; one needs a bot token to verify) and dropped one mid-analysis when its data store hung at the filesystem level inconsistently across tools. Excluding a broken task is more honest than scoring noise.

## Results

Success, cost, and disturbance (focus switches · cursor moves), summed over the trials:

| Tool | Success | Total cost | Disturbance (focus · cursor) |
|---|---|---|---|
| **Hunch** | **53/54** | **$6.03** | **14 · 3** |
| Peekaboo | 41/53 | $31.03 | 80 · 46 |
| cua-driver | 31/54 | $37.42 | 97 · 71 |

<figure>
<div class="chartcard"><svg viewBox="0 0 660 470" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Scatter plot of screen disturbance per task: focus switches on the x-axis, cursor moves on the y-axis. Hunch clusters at the origin; cua-driver and Peekaboo scatter far out." font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif">
<text x="330" y="26" text-anchor="middle" fill="#e5e7eb" font-size="17" font-weight="600">Screen disturbance per task (n=5 totals)</text>
<line x1="70" y1="50" x2="70" y2="400" stroke="#374151" stroke-width="1"/>
<text x="70" y="420" text-anchor="middle" fill="#9ca3af" font-size="12">0</text>
<line x1="178" y1="50" x2="178" y2="400" stroke="#374151" stroke-width="1"/>
<text x="178" y="420" text-anchor="middle" fill="#9ca3af" font-size="12">5</text>
<line x1="285" y1="50" x2="285" y2="400" stroke="#374151" stroke-width="1"/>
<text x="285" y="420" text-anchor="middle" fill="#9ca3af" font-size="12">10</text>
<line x1="392" y1="50" x2="392" y2="400" stroke="#374151" stroke-width="1"/>
<text x="392" y="420" text-anchor="middle" fill="#9ca3af" font-size="12">15</text>
<line x1="500" y1="50" x2="500" y2="400" stroke="#374151" stroke-width="1"/>
<text x="500" y="420" text-anchor="middle" fill="#9ca3af" font-size="12">20</text>
<line x1="70" y1="400" x2="500" y2="400" stroke="#374151" stroke-width="1"/>
<text x="60" y="404" text-anchor="end" fill="#9ca3af" font-size="12">0</text>
<line x1="70" y1="303" x2="500" y2="303" stroke="#374151" stroke-width="1"/>
<text x="60" y="307" text-anchor="end" fill="#9ca3af" font-size="12">10</text>
<line x1="70" y1="206" x2="500" y2="206" stroke="#374151" stroke-width="1"/>
<text x="60" y="210" text-anchor="end" fill="#9ca3af" font-size="12">20</text>
<line x1="70" y1="108" x2="500" y2="108" stroke="#374151" stroke-width="1"/>
<text x="60" y="112" text-anchor="end" fill="#9ca3af" font-size="12">30</text>
<text x="285" y="446" text-anchor="middle" fill="#e5e7eb" font-size="13">Focus switches (app raised) - more disruptive</text>
<text x="24" y="225" text-anchor="middle" fill="#e5e7eb" font-size="13" transform="rotate(-90 24 225)">Cursor moves</text>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="177.5" cy="390.3" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="134.5" cy="380.6" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="199.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e" stroke-width="1"/>
<circle cx="328.0" cy="322.2" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="156.0" cy="400.0" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="156.0" cy="390.3" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="435.5" cy="254.2" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="435.5" cy="351.4" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="177.5" cy="341.7" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="306.5" cy="390.3" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="113.0" cy="390.3" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="199.0" cy="400.0" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="91.5" cy="351.4" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="91.5" cy="361.1" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6" stroke-width="1"/>
<circle cx="457.0" cy="361.1" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="220.5" cy="341.7" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="177.5" cy="263.9" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="220.5" cy="390.3" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="220.5" cy="400.0" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="392.5" cy="59.7" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="70.0" cy="400.0" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="328.0" cy="400.0" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="220.5" cy="400.0" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="478.5" cy="293.1" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444" stroke-width="1"/>
<circle cx="540" cy="70" r="6.5" fill="#22c55e" fill-opacity="0.72" stroke="#22c55e"/>
<text x="554" y="74" fill="#e5e7eb" font-size="13">Hunch  53/54</text>
<circle cx="540" cy="94" r="6.5" fill="#3b82f6" fill-opacity="0.72" stroke="#3b82f6"/>
<text x="554" y="98" fill="#e5e7eb" font-size="13">Peekaboo  41/53</text>
<circle cx="540" cy="118" r="6.5" fill="#ef4444" fill-opacity="0.72" stroke="#ef4444"/>
<text x="554" y="122" fill="#e5e7eb" font-size="13">cua-driver  31/54</text>
<text x="536" y="156" fill="#9ca3af" font-size="11">each dot = one task</text>
<text x="536" y="172" fill="#9ca3af" font-size="11">lower-left = less</text>
<text x="536" y="188" fill="#9ca3af" font-size="11">disturbance</text>
<text x="536" y="216" fill="#22c55e" font-size="11.5" font-weight="600">8 of Hunch’s 11</text>
<text x="536" y="231" fill="#22c55e" font-size="11.5" font-weight="600">tasks land at 0,0</text>
</svg></div>
<figcaption>Screen disturbance per task across 11 tasks (n=5 totals). Lower-left is better. Hunch registers 0 focus / 0 cursor on all eight native-API, cross-app, and web tasks; cua-driver and Peekaboo scatter into cursor-dragging and app-raising. Source: mac-agent-bench, 2026-07-27.</figcaption>
</figure>

Hunch finishes first on every axis at once: highest success, ~6.2x/~5.1x lower cost, and far less screen disturbance. Hunch registered **0 focus switches and 0 cursor moves on all eight native-API, cross-app, and web tasks**; its entire footprint (14 focus switches, 3 cursor moves) came from the three tiers that inherently touch the GUI, namely the accessibility toggle, window tiling, and the Spotify app. That's what native APIs and background accessibility buy you on a *host-native* suite. The number that carries the argument isn't the win rate; it's the disturbance column, because it's the one no other benchmark reports.

<!-- [ORIGINAL DATA] all three columns are first-hand measurements from our own harness -->

## The surprise: the "focus-free twin" degrades under load

The result we didn't expect came from cua-driver, the one competitor that makes the *same* claim we do. It drives the host, not a VM, and targets windows through Apple's SkyLight layer specifically to avoid stealing focus. On paper it's our architectural twin.

And on simple actions, the claim holds: pressing one accessibility control, creating a note, and playing a track all came in at **0 · 0**, genuinely focus-free.

Then the tasks got real. Sorting files, tiling windows, chaining steps, and the focus-free path collapsed. It fell back to **cursor-dragging and raising apps to the foreground**, ending the sweep with the *most* disturbance of any tool (97 focus switches, 71 cursor moves), the highest cost ($37.42), and the most timeouts (20 of its runs hit the wall). With no OS-API layer to fall back to, it couldn't complete some tasks a single API call handles.

The lesson generalizes past our tool: **a focus-free layer is only worth having if it survives real work.** "Focus-free" measured on a button press is marketing; measured across a task suite it's an engineering property, and it's one you have to design the whole stack for, not bolt onto a screenshot loop.

## How Hunch stays out of your way

The 0 focus / 0 cursor columns are not a trick of the harness; they fall out of the architecture. An MCP client sees Hunch as a set of tools, and a developer imports its SDK object directly, but both are apps on the same core, and every action runs through one engine that tries the most direct layer first: an OS API, then AppleScript, then the web (Chrome DevTools), then the accessibility tree, and only as a gated last resort, vision. A screenshot-and-click loop starts at the bottom of that ladder; Hunch starts at the top, which is why it can drive a real Mac without raising an app or moving the pointer.

<div class="arch">
<div class="row">
<div class="box"><b>MCP client</b><small>Claude Desktop / Claude Code</small></div>
<div class="box"><b>Your Python app</b><small>imports the SDK object directly</small></div>
</div>
<div class="arrow">↓</div>
<div class="box"><b>One engine: _dispatch_core</b><small>the single tool-execution core shared by every face · consent + focus-free gate · Keychain auth</small></div>
<div class="arrow">↓</div>
<div class="box"><b>Execution backends</b><small>local_mac + ax_tree (AX read + actions) · cdp (web / Electron) · os_ops (files · clipboard · AppleScript)</small></div>
<div class="arrow">↓</div>
<div class="ladline">OS-API → AppleScript → Web / CDP → Accessibility → Vision (gated last resort)</div>
<div class="arrow">↓</div>
<div class="box"><b>Real macOS</b><small>background apps &amp; windows, no stolen cursor or foreground</small></div>
<div class="cap">The focus-free ladder: every call enters one engine and takes the most direct layer first.</div>
</div>

## What we'd caveat

Publishing a benchmark your own tool wins demands showing the seams:

- **Small n and one machine.** These are confirmatory n=5 results on one model (Claude Sonnet) and one Apple Silicon Mac. Directional, not a universal leaderboard.
- **Timeouts bill $0.** A timed-out run never emits a cost event, so it counts as zero. The slower tools timed out more, which means their cost totals are **floors**: the gap is understated, not inflated.
- **Environment noise and checker edges.** One task's data store hung (excluded); one setup timed out per tool; and Hunch's lone non-pass was a checker false-negative, where Spotify was playing the correct track but returned an empty artist field. None is scored as a capability difference.
- **Scope.** This is a *host-native, focus-free-weighted* suite. On a cross-OS or VM suite the ranking would look different, and we'd say so. We're measuring the axis we think matters for an agent on your actual machine, not claiming universal superiority.

## Run it yourself

The point of a vendor benchmark is that nobody has to trust the vendor. The harness is "same brain, different hands": an adapter is a few lines of JSON pointing at an MCP server, and adding your own tool or task is a pull request. If you build a computer-use agent, submit it as an adapter and tell us where our numbers are wrong; we'd rather find out from you than from a reader.

## Conclusion

The interesting question about a computer-use agent on your own Mac isn't only "can it do the task," it's "can it do the task without taking the machine away from me." That's measurable, it's the axis the VM benchmarks structurally can't see, and when you measure it the differences are stark, including a competitor whose focus-free promise held only until the work got hard. If you want an agent that drives your Mac while you keep using it, that's the whole design goal of Hunch, and now there's a number for it.

## FAQ

### Why not just use an existing macOS agent benchmark?
They run in VMs and score only task success. None measures host disturbance (whether the agent steals your cursor and foreground), which is the property that decides if you'd run one on your real machine.

### Is "same brain, different hands" fair to the other tools?
It holds the model constant and varies only the MCP tool layer, so differences are attributable to the hands. The trade-off: a tool that ships its own tuned agent loop is being driven by our shared model instead, which we disclose. It's the fairest apples-to-apples we found; alternatives (each tool's own agent + model) measure agent loops, not tools.
