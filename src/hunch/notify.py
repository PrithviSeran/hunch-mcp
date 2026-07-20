"""Desktop notifications, wearing the Hunch logo when possible.

macOS's `display notification` (osascript) cannot show a custom icon — it always
wears the sender's (Script Editor's). When `terminal-notifier` is on PATH (the
Homebrew formula depends on it), notifications go through it with the bundled
logo; otherwise we fall back to plain osascript. Stdlib-only on purpose: the CLI
imports this without pyobjc.
"""

import os
import shutil
import subprocess

ICON = os.path.join(os.path.dirname(__file__), "assets", "hunch.png")


def _as_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message, title="Hunch", sound=None, timeout=5):
    """Best-effort desktop notification; never raises."""
    tn = shutil.which("terminal-notifier")
    if tn and os.path.exists(ICON):
        args = [tn, "-title", title, "-message", str(message),
                "-appIcon", ICON, "-contentImage", ICON]
        if sound:
            args += ["-sound", sound]
        try:
            subprocess.run(args, check=False, capture_output=True, timeout=timeout)
            return
        except Exception:
            pass
    try:
        script = f"display notification {_as_str(message)} with title {_as_str(title)}"
        if sound:
            script += f" sound name {_as_str(sound)}"
        subprocess.run(["osascript", "-e", script], check=False, timeout=timeout)
    except Exception:
        pass
