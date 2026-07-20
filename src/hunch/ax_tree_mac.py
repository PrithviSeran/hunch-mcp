#!/usr/bin/env python3
"""
ax_tree_mac.py — minimal macOS Accessibility (AXUIElement) tree explorer.

Usage:
    python3 ax_tree_mac.py list                      # list running apps with a UI
    python3 ax_tree_mac.py dump <app_name>            # snapshot: focused window + menu titles
    python3 ax_tree_mac.py dump <app_name> --max-depth 6
    python3 ax_tree_mac.py dump                       # snapshot ALL running apps to ax_trees.json
    python3 ax_tree_mac.py dump --format text         # indented outline, most token-efficient
    python3 ax_tree_mac.py dump --full                # old behavior: whole tree from the app root
    python3 ax_tree_mac.py dump Mail --activate       # raise the app if its window is unreachable
    python3 ax_tree_mac.py dump --pretty -o out.json  # human-readable JSON

By default, dump snapshots what's actionable now: the focused window's subtree
(resolved via AXFocusedWindow, which works even when the window is fullscreen or
on another Space — AXChildren/AXWindows miss those) plus menu bar titles only.
Closed menu contents are not serialized.

Output is LLM-friendly: compact JSON with empty/null fields omitted. Every node
keeps its "path" (child indices from the window root, e.g. "0/3/2") as a handle
for referencing nodes. --format text emits an indented outline (~2x fewer tokens
than compact JSON) with the same information.

Requires:
    pip3 install pyobjc-framework-ApplicationServices pyobjc-framework-Quartz pyobjc-framework-Cocoa

You MUST grant Accessibility permission to whatever process runs this script:
System Settings -> Privacy & Security -> Accessibility -> enable Terminal (or your IDE / python binary).
"""

import sys
import json
import time
import argparse
import subprocess

from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementSetMessagingTimeout,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyMultipleAttributeValues,
    AXUIElementCopyAttributeNames,
    AXUIElementPerformAction,
    AXValueGetType,
    AXValueGetTypeID,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from CoreFoundation import CFGetTypeID

try:
    from ApplicationServices import kAXValueAXErrorType
except ImportError:
    kAXValueAXErrorType = 5
from AppKit import (
    NSWorkspace,
    NSRunningApplication,
    NSApplicationActivateIgnoringOtherApps,
)
from Foundation import NSObject
import Quartz

# Attribute constants (as raw strings; PyObjC often exposes these directly as CFStrings)
kAXChildrenAttribute = "AXChildren"
kAXRoleAttribute = "AXRole"
kAXTitleAttribute = "AXTitle"
kAXValueAttribute = "AXValue"
kAXDescriptionAttribute = "AXDescription"
kAXPositionAttribute = "AXPosition"
kAXSizeAttribute = "AXSize"
kAXEnabledAttribute = "AXEnabled"
kAXMenuBarAttribute = "AXMenuBar"
kAXFocusedWindowAttribute = "AXFocusedWindow"
kAXMainWindowAttribute = "AXMainWindow"
kAXWindowsAttribute = "AXWindows"


def list_apps():
    """List running GUI apps (activation policy == regular) as {name, pid}.

    NSWorkspace.runningApplications() is a KVO-updated array that only refreshes when a Cocoa run
    loop is spinning — in a long-lived non-Cocoa process (the MCP server) it stays FROZEN at first
    read, so any app launched afterwards is invisible (snapshot(app) then reports "not found").
    So we source live pids FRESH from the window server (CGWindowList — owner name/pid need no Screen
    Recording permission) and resolve each by pid (runningApplicationWithProcessIdentifier_ is a fresh
    per-pid lookup), then union with the NSWorkspace list to also catch windowless regular apps."""
    seen = {}   # pid -> display name
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
        for w in info:
            pid = w.get("kCGWindowOwnerPID")
            owner = w.get("kCGWindowOwnerName")
            if not pid or pid in seen:
                continue
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None and app.activationPolicy() == 0:
                seen[pid] = app.localizedName() or owner
    except Exception:
        pass
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() == 0 and app.processIdentifier() not in seen:
            seen[app.processIdentifier()] = app.localizedName()
    return [{"name": n, "pid": p} for p, n in seen.items()]


def get_attr(element, attr):
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    if err != 0:
        return None
    return value


_AX_VALUE_TYPE_ID = AXValueGetTypeID()


def _is_ax_error(v):
    # With options=0, CopyMultipleAttributeValues reports per-attribute failures
    # as AXValue placeholders of the error type rather than omitting them.
    return (v is not None
            and CFGetTypeID(v) == _AX_VALUE_TYPE_ID
            and AXValueGetType(v) == kAXValueAXErrorType)


def get_attrs(element, attrs):
    """Read several attributes in one IPC round-trip. Per-attribute reads cost
    one synchronous message to the target app each; on huge trees (Mail's inbox
    is one AXRow per email) that's the difference between seconds and minutes."""
    err, values = AXUIElementCopyMultipleAttributeValues(element, list(attrs), 0, None)
    if err != 0 or values is None:
        return {a: get_attr(element, a) for a in attrs}
    return {a: (None if _is_ax_error(v) else v) for a, v in zip(attrs, values)}


def values_to_bounds(pos, size):
    if pos is None or size is None:
        return None
    try:
        ok1, pt = AXValueGetValue(pos, kAXValueCGPointType, None)
        ok2, sz = AXValueGetValue(size, kAXValueCGSizeType, None)
        if not (ok1 and ok2):
            return None
        return {"x": pt.x, "y": pt.y, "w": sz.width, "h": sz.height}
    except Exception:
        return None


_NODE_ATTRS = (
    kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
    kAXValueAttribute, kAXEnabledAttribute, kAXPositionAttribute,
    kAXSizeAttribute, kAXChildrenAttribute,
)

DEFAULT_MAX_NODES = 10000
DEFAULT_MAX_CHILDREN = 200


def node_to_dict(element, depth, max_depth, path="", budget=None, max_children=None):
    if budget is None:
        budget = {"left": DEFAULT_MAX_NODES}
    if max_children is None:
        max_children = DEFAULT_MAX_CHILDREN
    budget["left"] -= 1

    a = get_attrs(element, _NODE_ATTRS)
    value = a[kAXValueAttribute]
    enabled = a[kAXEnabledAttribute]

    node = {
        "path": path,
        "role": str(a[kAXRoleAttribute] or "unknown"),
        "title": str(a[kAXTitleAttribute] or ""),
        "description": str(a[kAXDescriptionAttribute] or ""),
        "value": str(value) if value is not None else None,
        "enabled": bool(enabled) if enabled is not None else None,
        "bounds": values_to_bounds(a[kAXPositionAttribute], a[kAXSizeAttribute]),
        "children": [],
    }

    children = a[kAXChildrenAttribute]
    if not children:
        return node
    if depth >= max_depth:
        # Children exist but weren't walked — mark it, so a depth-capped node
        # is distinguishable from a genuine leaf.
        node["truncated_depth"] = True
        return node

    kids = list(children)
    dropped = 0
    if len(kids) > max_children:
        dropped = len(kids) - max_children
        kids = kids[:max_children]

    for i, child in enumerate(kids):
        if budget["left"] <= 0:
            dropped += len(kids) - i
            break
        child_path = f"{path}/{i}" if path else str(i)
        node["children"].append(node_to_dict(child, depth + 1, max_depth, child_path, budget, max_children))

    if dropped:
        node["truncated_children"] = dropped

    return node


def get_window(ax_app):
    """Resolve the app's current window. AXChildren/AXWindows do NOT enumerate
    windows on other Spaces (fullscreen apps, Mission Control spaces), so walking
    from the app root silently misses them — but AXFocusedWindow still resolves."""
    win = get_attr(ax_app, kAXFocusedWindowAttribute) or get_attr(ax_app, kAXMainWindowAttribute)
    if win is None:
        windows = get_attr(ax_app, kAXWindowsAttribute)
        if windows:
            win = windows[0]
    if win is None:
        # Catalyst/UIKit apps (WhatsApp) often report an empty AXWindows and nil focused/main window
        # while backgrounded, yet the window element is still present under AXChildren. Find it there
        # before giving up — otherwise snapshot returns "no window" and the caller wrongly falls back
        # to launch/focus/screenshots when the tree was actually reachable.
        for child in (get_attr(ax_app, kAXChildrenAttribute) or []):
            if str(get_attr(child, kAXRoleAttribute) or "") == "AXWindow":
                win = child
                break
    return win


def ensure_window(pid, timeout=4.0):
    """Make the app's window AX-reachable if it isn't (minimized, or parked on
    another Space without app focus). Activates the app, and if that alone
    doesn't surface a window, sends it a reopen event (same as clicking its
    Dock icon), which restores minimized windows or opens a fresh one.
    Returns True once get_window() would succeed."""
    ax_app = AXUIElementCreateApplication(pid)
    if get_window(ax_app) is not None:
        return True

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return False
    activate_app(app)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_window(ax_app) is not None:
            return True
        time.sleep(0.1)
    return False


def activate_app(app):
    """Bring an app to the front. Goes through Launch Services ('open'), which
    also sends a reopen event (restores minimized windows / opens a fresh one).
    NSRunningApplication.activateWithOptions_ is silently ignored when called
    from a background CLI on recent macOS, so it's only the fallback."""
    bundle_id = app.bundleIdentifier()
    if bundle_id:
        subprocess.run(["open", "-b", bundle_id], check=False)
    else:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


def snapshot_app(name, pid, max_depth=10):
    """Snapshot what's actionable now: focused-window subtree + menu bar titles.
    Skips the contents of closed menus entirely."""
    ax_app = AXUIElementCreateApplication(pid)
    entry = {"name": name, "pid": pid}

    menubar = get_attr(ax_app, kAXMenuBarAttribute)
    if menubar is not None:
        items = get_attr(menubar, kAXChildrenAttribute) or []
        entry["menus"] = [str(get_attr(i, kAXTitleAttribute) or "") for i in items]

    win = get_window(ax_app)
    entry["window"] = node_to_dict(win, 0, max_depth) if win is not None else None
    return entry


def full_dump_app(name, pid, max_depth=10):
    ax_app = AXUIElementCreateApplication(pid)
    return {"name": name, "pid": pid, "tree": node_to_dict(ax_app, 0, max_depth)}


def dump_app(app_name, max_depth=10, full=False, activate=False):
    apps = list_apps()
    match = next((a for a in apps if a["name"] == app_name), None)
    if match is None:
        raise SystemExit(f"App '{app_name}' not found or has no UI. Run 'list' to see options.")

    if activate and not ensure_window(match["pid"]):
        print(f"Warning: could not surface a window for {match['name']}", file=sys.stderr)
    fn = full_dump_app if full else snapshot_app
    return fn(match["name"], match["pid"], max_depth)


def strip_node(node):
    """Drop empty/null fields and empty children lists. Lossless: a missing key
    means "no value", and token-friendly for LLM consumption."""
    out = {}
    for k, v in node.items():
        if k == "children":
            kids = [strip_node(c) for c in v]
            if kids:
                out["children"] = kids
        elif v not in (None, ""):
            out[k] = v
    return out


def node_to_text(node, depth=0):
    """Render a node as an indented text outline — about half the tokens of
    compact JSON. The [path] tag gives LLMs a stable handle to each node."""
    parts = [node.get("role", "unknown")]
    if node.get("title"):
        parts.append(f'"{node["title"]}"')
    if node.get("description"):
        parts.append(f'({node["description"]})')
    if node.get("value") not in (None, ""):
        parts.append(f'val={node["value"]!r}')
    if node.get("enabled") is False:
        parts.append("disabled")
    b = node.get("bounds")
    if b:
        parts.append(f'@{int(b["x"])},{int(b["y"])} {int(b["w"])}x{int(b["h"])}')
    parts.append(f'[{node.get("path") or "root"}]')
    lines = ["  " * depth + " ".join(parts)]
    for child in node.get("children", []):
        lines.extend(node_to_text(child, depth + 1))
    if node.get("truncated_children"):
        lines.append("  " * (depth + 1) + f"… +{node['truncated_children']} more children truncated")
    if node.get("truncated_depth"):
        lines.append("  " * (depth + 1) + "… children not walked (max depth reached)")
    return lines


def render(entries, fmt, pretty=False):
    if fmt == "text":
        lines = []
        for e in entries:
            lines.append(f'=== {e["name"]} (pid {e["pid"]}) ===')
            if e.get("menus"):
                lines.append("menus: " + " | ".join(m for m in e["menus"] if m))
            tree = e.get("tree") or e.get("window")
            if tree is not None:
                lines.extend(node_to_text(tree))
            elif "window" in e:
                lines.append("(no window)")
        return "\n".join(lines) + "\n"

    stripped = []
    for e in entries:
        out = {k: v for k, v in e.items() if k not in ("tree", "window")}
        if e.get("tree") is not None:
            out["tree"] = strip_node(e["tree"])
        if "window" in e:
            out["window"] = strip_node(e["window"]) if e["window"] is not None else None
        stripped.append(out)
    if pretty:
        return json.dumps(stripped, indent=2) + "\n"
    return json.dumps(stripped, separators=(",", ":")) + "\n"


def dump_all_apps(max_depth=10, full=False, activate=False):
    fn = full_dump_app if full else snapshot_app
    result = []
    for app in list_apps():
        print(f"Dumping {app['name']} (pid {app['pid']})...", file=sys.stderr)
        if activate and not ensure_window(app["pid"]):
            print(f"Warning: could not surface a window for {app['name']}", file=sys.stderr)
        result.append(fn(app["name"], app["pid"], max_depth))
    return result


def main():
    global DEFAULT_MAX_NODES, DEFAULT_MAX_CHILDREN
    parser = argparse.ArgumentParser(description="macOS AXUIElement tree explorer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List running apps with a UI")

    dump_parser = sub.add_parser("dump", help="Dump the AX tree of an app (or all apps if no name given)")
    dump_parser.add_argument("app_name", nargs="?", default=None,
                             help="Exact app name as shown by 'list' (e.g. 'Safari'). Omit to dump all running apps.")
    dump_parser.add_argument("--max-depth", type=int, default=10,
                             help="Max tree depth to walk; deeper nodes are marked "
                                  "truncated_depth (default %(default)s)")
    dump_parser.add_argument("--max-children", type=int, default=DEFAULT_MAX_CHILDREN,
                             help="Cap children serialized per node; excess is reported as "
                                  "truncated_children (default %(default)s)")
    dump_parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES,
                             help="Cap total nodes per dumped tree (default %(default)s)")
    dump_parser.add_argument("--full", action="store_true",
                             help="Walk the entire tree from the app root (old behavior). "
                                  "Misses windows on other Spaces and dumps closed menu contents.")
    dump_parser.add_argument("--activate", action="store_true",
                             help="If an app's window is not AX-reachable (minimized or on "
                                  "another Space), bring the app forward to restore it before "
                                  "snapshotting. Restores your previous frontmost app afterward.")
    dump_parser.add_argument("--format", choices=["json", "text"], default="json",
                             help="json: compact JSON with empty fields omitted; "
                                  "text: indented outline, ~2x fewer tokens (best for LLMs)")
    dump_parser.add_argument("--pretty", action="store_true",
                             help="Pretty-print JSON with indentation (human-readable, ~5x larger)")
    dump_parser.add_argument("-o", "--output", default=None,
                             help="Output file (default: ax_trees.json/.txt when dumping all apps, stdout for a single app)")

    args = parser.parse_args()

    # Cap how long any single AX call may block on an unresponsive app
    # (system default is 6s per call; on the system-wide element this applies
    # to every AX call this process makes).
    AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 2.0)

    if args.command == "list":
        for app in list_apps():
            print(f"{app['name']} (pid {app['pid']})")
    elif args.command == "dump":
        DEFAULT_MAX_NODES = args.max_nodes
        DEFAULT_MAX_CHILDREN = args.max_children
        previous_front = NSWorkspace.sharedWorkspace().frontmostApplication() if args.activate else None

        if args.app_name is None:
            entries = dump_all_apps(args.max_depth, args.full, args.activate)
            ext = "txt" if args.format == "text" else "json"
            output = args.output or f"ax_trees.{ext}"
        else:
            entries = [dump_app(args.app_name, args.max_depth, args.full, args.activate)]
            output = args.output

        if previous_front is not None:
            activate_app(previous_front)

        rendered = render(entries, args.format, args.pretty)
        if output:
            with open(output, "w") as f:
                f.write(rendered)
            print(f"Wrote {output}", file=sys.stderr)
        else:
            sys.stdout.write(rendered)


if __name__ == "__main__":
    main()