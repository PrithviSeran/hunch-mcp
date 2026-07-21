#!/usr/bin/env python3
"""Run one natural-language task on this Mac with the Hunch agent loop.

    pip install 'hunch-mcp[agent]'
    export ANTHROPIC_API_KEY=sk-ant-...        # or: ant auth login
    python examples/agent_task.py "open Music and play my Focus playlist"

Your terminal/IDE needs the Accessibility permission (System Settings → Privacy &
Security → Accessibility). By default Claude asks before any focus-stealing or
irreversible step (a real dialog); pass --unattended to auto-approve for scripts.
"""
import argparse

from hunch import Hunch


def main():
    p = argparse.ArgumentParser()
    p.add_argument("task")
    p.add_argument("--model", default="claude-opus-4-8")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--effort", default=None, help="low | medium | high | xhigh | max")
    p.add_argument("--unattended", action="store_true",
                   help="auto-approve gated actions (no dialogs) — you accept the risk")
    args = p.parse_args()

    def on_event(kind, data):
        if kind == "text":
            print(f"\nclaude: {data}")
        elif kind == "tool":
            print(f"  → {data['name']}({data['input']})")
        elif kind == "tool_result":
            print(f"    {data}")
        elif kind == "done":
            print(f"\n✓ {data}")
        elif kind == "error":
            print(f"\n✗ {data}")

    mac = Hunch(confirm="off" if args.unattended else "dialog")
    try:
        result = mac.agent.run(args.task, model=args.model, max_turns=args.max_turns,
                               effort=args.effort, on_event=on_event)
        print(f"\n— {result.turns} turns, stop_reason={result.stop_reason}, "
              f"tokens in/out={result.usage.get('input_tokens')}/{result.usage.get('output_tokens')} "
              f"(cached read {result.usage.get('cache_read_input_tokens')})")
    finally:
        mac.close()


if __name__ == "__main__":
    main()
