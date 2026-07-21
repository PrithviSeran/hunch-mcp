#!/usr/bin/env python3
"""Read Mail.app's inbox focus-free — a deterministic script, no LLM involved.

Run with the Python that has hunch-sdk installed. The app running this script
(your terminal or IDE) needs the Accessibility permission:
System Settings → Privacy & Security → Accessibility.
"""
from hunch import Hunch, AccessibilityNotGranted

try:
    mac = Hunch()
except AccessibilityNotGranted as e:
    raise SystemExit(f"Setup needed: {e}")

tree = mac.snapshot("Mail")
if tree.startswith("(app '"):
    raise SystemExit("Mail isn't running — open it once (or mac.launch_app('Mail')) and retry.")

# The tree is one element per line; inbox rows and unread counts are plain text in it.
interesting = [ln for ln in tree.splitlines()
               if "unread" in ln.lower() or "inbox" in ln.lower()]
print("\n".join(interesting) if interesting
      else "No inbox/unread rows visible — click into the Inbox and re-run.\n\n" + tree[:800])
