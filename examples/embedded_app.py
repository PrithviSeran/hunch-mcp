#!/usr/bin/env python3
"""What shipping an app ON the Hunch SDK looks like — every surface is yours.

An identified instance gets its OWN storage (Keychain slots, browser profile, CDP
port), so it coexists with other Hunch-based apps and the user's personal Hunch
without any of them touching each other. Consent renders through YOUR callback,
notifications through YOUR handler, and the agent loop uses ONLY the credential
you inject — nothing is scavenged from the user's machine.

Run with the Python that has hunch-sdk installed; the app running this script
needs the Accessibility permission.
"""
from hunch import Hunch, ConsentRequest, OAuthToken


def ask_user(req: ConsentRequest) -> bool:
    """Your app's consent UI (a sheet, a push notification, a Slack DM...).
    Here: a terminal prompt. Return True to approve; exceptions count as denial."""
    print(f"\n[consent:{req.category}] {req.prompt}")
    return input("approve? [y/N] ").strip().lower() == "y"


def toast(message: str, title: str) -> None:
    """Your app's notification surface instead of macOS banners."""
    print(f"\n🔔 {title}: {message}")


mac = Hunch(
    provider="claude",                  # which LLM vendor drives mac.agent ("claude" | "codex")
    app_id="com.example.demoapp",       # pure namespacing: own Keychain slots, own
                                        #   browser profile, own derived CDP port
    app_name="Demo App",                # what consent prompts + notifications say
    confirm=ask_user,                   # consent goes through YOUR UI
    notify=toast,                       # notifications go through YOUR surface
    policy={"gates": {"shell": True}},  # instance-owned safety; the user's personal
                                        #   ~/.hunch/config.json is never consulted
    auth=OAuthToken("sk-ant-oat-...replace-me..."),  # the exact Claude subscription token YOUR
                                                     #   app manages (redacted repr) — not the user's
)

print(mac.snapshot("Finder").splitlines()[0])   # deterministic primitives work as usual
print(mac.list_credentials())                    # this app's OWN (empty) credential store
mac.notify("Demo App is wired up.")

# With the injected token above, the agent loop runs on YOUR credential:
#   mac.agent.run("file the invoices in ~/Downloads")
# (Codex apps authenticate via `codex login`; no token to inject.)
mac.close()
