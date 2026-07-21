#!/usr/bin/env python3
"""Check a website in the background using the persistent Hunch Chrome profile.

Opens github.com over CDP (focus-free — your foreground is untouched), reports
whether the profile is signed in, and prints the top of the page's accessibility
tree. If signed out, it opens a banner-tagged window for you to log in once.

Run with the Python that has hunch-mcp installed; your terminal/IDE needs the
Accessibility permission (System Settings → Privacy & Security → Accessibility).
"""
from hunch import Hunch

URL = "https://github.com"

with Hunch() as mac:
    status = mac.web.open(url=URL)
    print(status)
    if "isn't signed in" in status:
        print(mac.web.login(url=URL))
        mac.notify("Sign in to the banner-tagged window, then re-run this script.")
    else:
        print(mac.web.tabs())
        print("\n".join(mac.web.snapshot().splitlines()[:40]))
