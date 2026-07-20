# Security

Hunch gives an LLM agent real control of a real Mac. That is the point — and the risk. This
document says exactly what protects you, what doesn't, and how to reason about it.

## Threat model

**Primary threat: prompt injection.** A background agent reads content its user did not write —
web pages, emails, documents. Any of it can contain instructions crafted to steer the model
("ignore your instructions, run this command, type the password here"). Because Hunch runs in the
background, the user may not be watching when it happens.

**Secondary threat: agent error.** A confused model deleting the wrong folder or sending the wrong
email, with no malice involved.

Out of scope: a malicious MCP *host* (it launches the server and owns its environment — game over
by construction), and local attackers with your user account.

## Trust boundaries

1. **Your MCP host's tool approvals** are the primary gate. Hunch assumes the model is untrusted;
   it does not assume the host is.
2. **Hunch's confirmation dialogs** are a content-aware second gate, in the tool layer itself, for
   the catastrophic categories:
   - `focus_steal` — keyboard/cursor takeover (`key`, `click_xy`, ref-less typing)
   - `app_to_front` — bringing an app to the front (a view switch, even mid-fullscreen)
   - `shell` — AppleScript containing `do shell script`
   - `destructive_applescript` — delete / send / empty trash / shut down / …
   A macOS dialog asks the human; the action is skipped on Cancel. Defaults: all on.
   One approval authorizes only its immediate follow-through (~15 s window): the approved
   switch isn't re-asked or re-notified, and approving a risky AppleScript deliberately does
   NOT count as screen consent. With `app_to_front` off, focus switches fall back to a
   desktop notification — asked about or announced, never both, never silent.
3. **Secrets never enter the model.** Credentials live in the macOS Keychain. The fill tools
   accept a service *name*, read the secret in-process, and type it into the page over CDP. The
   agent learns only "filled" or a refusal.
4. **Domain binding.** A credential bound at add-time (`hunch creds add x --domain x.com`) is
   refused on any other host — the anti-phishing line when an injected agent lands on a look-alike
   login page. Unbound credentials (legacy) fill anywhere; bind them.
5. **Self-protection.** The agent-facing file tools (`trash`, `file_op`) refuse any path under
   `~/.hunch/` — the agent cannot rewrite its own gate policy or credential metadata. Policy
   changes go through the `hunch` CLI a human runs in a terminal.

## What disables the gates

- `hunch config set auto_approve_all on` — requires typing `yes` past an explicit warning.
- `HUNCH_NO_INTERNAL_GATE=1` — for embedding apps that provide their own approval UX. If you set
  this without providing one, you have removed the second gate; the host approvals are all that's
  left. `hunch doctor` warns loudly when either is active.

## Known limits (deliberately documented)

- **The shell gate is the fence around self-protection.** `do shell script` could touch `~/.hunch`
  — that call is exactly what the `shell` gate confirms with the human. Disable that gate and the
  self-protection can be routed around.
- **AppleScript risk detection is keyword-based.** It is a heuristic safety net, not a sandbox; an
  exotic script could evade classification. The host-approval layer still applies.
- **Keychain writes pass the secret as a `security` argv**, briefly visible to local `ps`. Local-
  only exposure; stdin-based storage is planned hardening.
- **Domain binding checks the page host**, not page integrity — it stops look-alike domains, not a
  compromise of the legitimate site itself.
- **Anything shown on screen** (via the gated vision fallback) reaches the model as pixels,
  including whatever else is visible in that app.

## Recommendations

- Keep all gates on. Bind every credential to domains.
- Use your host's tool-approval mode for destructive tools rather than blanket auto-approve.
- Give the web layer its own logged-in profile (`hunch setup` does this) and sign it into only the
  sites you want agents using.
- Treat `auto_approve_all` as "I am watching this run live."

## Reporting a vulnerability

Please report privately via GitHub Security Advisories on this repository (preferred) or email
prithviseran0@gmail.com. We'll acknowledge within a few days. Please don't open public issues for
exploitable problems.
