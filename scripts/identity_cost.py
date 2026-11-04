"""Cost of CDPSession.identity over real CDP: a headless Chrome on a throwaway profile, a public
page, every ref of one snapshot read once. Prints per-call ms (p50, p95, max), how many refs
carried keys, and a few examples. Read-only: no clicks."""
import os, statistics, subprocess, sys, tempfile, time, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from hunch.cdp import CDPSession  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = int(os.environ.get("PORT", "9444"))


def main(url, limit=120):
    prof = tempfile.mkdtemp(prefix="hunch-identity-")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                             "--no-first-run", "--no-default-browser-check", "--window-size=1400,1000", url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        s = CDPSession(PORT).connect()
        s.wait_ready()
        time.sleep(4)                                   # let the app render its lists
        text, meta = s.snapshot(max_nodes=3000)
        refs = list(s.registry)[:limit]
        t0 = time.perf_counter()
        times, out = [], {}
        for r in refs:
            a = time.perf_counter()
            out[r] = s.identity(r)
            times.append((time.perf_counter() - a) * 1000)
        total = (time.perf_counter() - t0) * 1000
        times.sort()
        keyed = [r for r in refs if out[r]["keys"]]
        print(f"{url}\n  refs read: {len(refs)} of {meta['refs']} in {total:.0f} ms"
              f"  per call p50 {statistics.median(times):.1f} ms  p95 {times[int(len(times) * .95) - 1]:.1f} ms  max {times[-1]:.1f} ms")
        print(f"  with keys: {len(keyed)}; framework: {out[refs[0]]['framework'] if refs else '-'}")
        lines = {l.split()[0].strip('[]'): l.strip() for l in text.splitlines() if l.strip().startswith('[')}
        for r in keyed[:6]:
            print(f"  {lines.get(r, r)[:70]:70}  keys={'/'.join(out[r]['keys'])[:80]}  attrs={out[r]['attrs']}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    for u in sys.argv[1:] or ["https://trello.com/b/nC8QJJoZ/trello-development-roadmap", "https://github.com/facebook/react/issues"]:
        main(u)
