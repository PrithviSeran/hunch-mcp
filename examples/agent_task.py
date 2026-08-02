#!/usr/bin/env python3
"""Run one natural-language task on this Mac with the Hunch agent loop.

    pip install 'hunch-sdk[subscription]'                        # Claude
    python -c 'import hunch; hunch.provider("claude").login()'   # sign in (no API key)
    python examples/agent_task.py "open Music and play my Focus playlist"

Signed into Claude Code on this Mac already? Skip the login line — it's picked up.
For OpenAI Codex: pip install --pre 'hunch-sdk[codex]', run `codex login`, then pass
`--provider codex`.

Your terminal/IDE needs the Accessibility permission (System Settings -> Privacy &
Security -> Accessibility). By default the model asks before any focus-stealing or
irreversible step (a real dialog); pass --unattended to auto-approve for scripts.
"""
import argparse

from hunch import Hunch


def main():
    p = argparse.ArgumentParser()
    p.add_argument("task")
    p.add_argument("--provider", default="claude", choices=["claude", "codex"],
                   help="which LLM vendor drives the loop")
    p.add_argument("--model", default=None,
                   help="default: the provider's own model")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--effort", default=None, help="low | medium | high | xhigh | max")
    p.add_argument("--unattended", action="store_true",
                   help="auto-approve gated actions (no dialogs) — you accept the risk")
    args = p.parse_args()

    def on_event(kind, data):
        if kind == "text":
            print(f"\n{args.provider}: {data}")
        elif kind == "tool":
            print(f"  → {data['name']}({data['input']})")
        elif kind == "tool_result":
            print(f"    {data}")
        elif kind == "done":
            print(f"\n✓ {data}")
        elif kind == "error":
            print(f"\n✗ {data}")

    mac = Hunch(provider=args.provider, confirm="off" if args.unattended else "dialog")
    try:
        result = mac.agent.run(args.task, model=args.model, max_turns=args.max_turns,
                               effort=args.effort, on_event=on_event)
        print(f"\n— {result.turns} turns, stop_reason={result.stop_reason}, usage={result.usage}")
    finally:
        mac.close()


if __name__ == "__main__":
    main()
