#!/usr/bin/env python3
"""Benchmark the AX tree walk: serial vs concurrent prefetch.

    python examples/bench_snapshot.py Mail --runs 3 --workers 0,8

Runs snapshot() against a real app with each worker count and prints wall-clock
times. Worker count 0 = today's serial walk; 8 = wave-BFS prefetch. Outputs
should be line-identical modulo live-UI drift (a warning is printed if not).
"""
import argparse
import sys
import time

sys.path.insert(0, "src")
from hunch.local_mac import MacSession  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("app")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--workers", default="0,8",
                   help="comma-separated worker counts (0 = serial)")
    args = p.parse_args()

    texts = {}
    for w in [int(x) for x in args.workers.split(",")]:
        s = MacSession(walk_workers=w)
        times = []
        text = ""
        for _ in range(args.runs):
            t0 = time.monotonic()
            text, info = s.snapshot(app_name=args.app)
            times.append(time.monotonic() - t0)
        texts[w] = text
        label = "serial" if w <= 1 else f"{w} workers"
        print(f"{label:>10}: mean {sum(times)/len(times):6.2f}s  "
              f"runs {['%.2f' % t for t in times]}  "
              f"lines {len(text.splitlines()):5d}  refs {info.get('refs', '?')}")

    vals = list(texts.values())
    if len(vals) == 2 and vals[0].splitlines()[1:] != vals[1].splitlines()[1:]:
        a, b = (v.splitlines()[1:] for v in vals)
        drift = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
        print(f"warning: outputs differ on {drift} line(s) — expected only if the "
              "live UI changed between runs")


if __name__ == "__main__":
    main()
