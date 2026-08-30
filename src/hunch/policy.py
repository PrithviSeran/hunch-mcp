"""Gating policy for Hunch's internal confirmation dialogs.

The MCP host's own tool-approval UX is the primary permission gate; these
dialogs are a content-aware second gate for the catastrophic cases (focus
steals, shell, destructive AppleScript). Policy lives in ~/.hunch/config.json,
edited via `hunch config` — it is read on every check so changes apply to a
running server immediately, and the file itself is shielded from the agent's
file tools (see _protected in server.py).

Precedence: HUNCH_NO_INTERNAL_GATE env (embedding hosts that run their own
approval UX, e.g. the Hunch desktop app) > auto_approve_all > per-category.
"""

import json
import os

CONFIG_PATH = os.path.expanduser("~/.hunch/config.json")

DEFAULT_GATES = {
    "focus_steal": True,             # act() key / click_xy / ref-less type
    "app_to_front": True,            # focus_app / foreground launch_app switching the user's view
    "shell": True,                   # applescript() containing 'do shell script'
    "destructive_applescript": True, # delete / send / shut down / empty trash / ...
    "destructive_file": True,        # trash / file_op move / copy (destructive filesystem ops)
}

CONFIG_KEYS = ["gates.focus_steal", "gates.app_to_front", "gates.shell",
               "gates.destructive_applescript", "gates.destructive_file", "auto_approve_all"]


def default_config() -> dict:
    return {"version": 1, "auto_approve_all": False, "auto_approve_warned": False,
            "gates": dict(DEFAULT_GATES)}


def load() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config root is not an object")
        return cfg
    except (OSError, ValueError):
        # Absent or corrupt config == all gates on (today's default behavior).
        return default_config()


def save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def gate_enabled(category: str) -> bool:
    if os.environ.get("HUNCH_NO_INTERNAL_GATE"):
        return False
    cfg = load()
    if cfg.get("auto_approve_all"):
        return False
    gates = cfg.get("gates", {})
    return bool(gates.get(category, DEFAULT_GATES.get(category, True)))


def get(cfg: dict, dotted: str):
    """Read 'gates.shell'-style keys from a config dict."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def set_key(cfg: dict, dotted: str, value: bool) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
