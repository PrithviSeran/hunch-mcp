"""Unit tests for the AX tree walk: prefetch parallelism, scoped snapshots,
truncation markers, and find() — all against a FAKE tree (no real apps, no AX
permission needed). We monkeypatch local_mac.ax.get_attrs (the same module
attribute _prefetch uses) to serve attribute dicts for plain-object elements.
"""
import threading

import hunch.local_mac as local_mac
from hunch.local_mac import MacSession, _cap_depth, _cap_nodes, _MAX_NODES, _MAX_DEPTH

ax = local_mac.ax
ROLE, TITLE, DESC = ax.kAXRoleAttribute, ax.kAXTitleAttribute, ax.kAXDescriptionAttribute
VALUE, ENABLED, CHILDREN = ax.kAXValueAttribute, ax.kAXEnabledAttribute, ax.kAXChildrenAttribute
POS, SIZE = ax.kAXPositionAttribute, ax.kAXSizeAttribute


class FakeEl:
    """A fake AX element: role/title/value plus children."""
    def __init__(self, role, title="", value="", desc="", children=()):
        self.role, self.title, self.value, self.desc = role, title, value, desc
        self.children = list(children)

    def attrs(self):
        return {ROLE: self.role, TITLE: self.title, DESC: self.desc,
                VALUE: self.value or None, ENABLED: True, POS: None, SIZE: None,
                CHILDREN: self.children or None}


class Fetcher:
    """Stands in for ax.get_attrs; thread-safe call counter; optional failure mode."""
    def __init__(self, fail_off_main=False):
        self.calls = 0
        self.lock = threading.Lock()
        self.fail_off_main = fail_off_main

    def __call__(self, el, attrs):
        with self.lock:
            self.calls += 1
        if self.fail_off_main and threading.current_thread() is not threading.main_thread():
            raise RuntimeError("no AX off the main thread today")
        if isinstance(el, FakeEl):
            return el.attrs()
        raise AssertionError(f"unexpected element {el!r}")


def _session(workers=0):
    s = MacSession.__new__(MacSession)
    s.registry, s._keymap, s._ref_keys = {}, {}, {}
    s._counter, s.snapshot_count, s._pid = 0, 0, None
    s._app_name = "FakeApp"
    s.walk_workers = workers
    return s


def _snapshot_tree(s, root, **kw):
    """Drive the walk exactly like MacSession.snapshot does, minus window resolution."""
    max_depth = _cap_depth(kw.get("max_depth"), _MAX_DEPTH)
    max_nodes = _cap_nodes(kw.get("max_nodes"), _MAX_NODES)
    s.registry = {}
    s.snapshot_count += 1
    lines = ["=== FakeApp ==="]
    cache = s._prefetch(root, max_depth, max_nodes, True)
    fetch = s._fetcher(cache)
    budget = {"left": max_nodes, "hit": False}
    s._walk(root, 0, "", lines, True, max_depth, budget, fetch=fetch)
    if budget["hit"]:
        lines.append(local_mac._TRUNC_FOOTER.format(n=max_nodes))
    return "\n".join(lines)


def _patched(fetcher):
    class _Ctx:
        def __enter__(self):
            self.old = ax.get_attrs
            ax.get_attrs = fetcher
            return fetcher

        def __exit__(self, *exc):
            ax.get_attrs = self.old
            return False
    return _Ctx()


def _big_tree():
    win = FakeEl("AXWindow", "Main")
    for g in range(3):
        grp = FakeEl("AXGroup")
        for b in range(4):
            grp.children.append(FakeEl("AXButton", f"Btn{g}-{b}"))
        row = FakeEl("AXRow", children=[FakeEl("AXStaticText", value=f"Row text {g}")])
        grp.children.append(row)
        win.children.append(grp)
    return win


def test_serial_and_parallel_identical():
    s_serial, s_par = _session(0), _session(8)
    with _patched(Fetcher()):
        t1 = _snapshot_tree(s_serial, _big_tree())
    with _patched(Fetcher()):
        t2 = _snapshot_tree(s_par, _big_tree())
    assert t1 == t2
    assert s_serial._keymap == s_par._keymap
    assert s_serial._ref_keys == s_par._ref_keys


def test_parallel_fetch_count_not_higher():
    f_serial, f_par = Fetcher(), Fetcher()
    with _patched(f_serial):
        _snapshot_tree(_session(0), _big_tree())
    with _patched(f_par):
        _snapshot_tree(_session(8), _big_tree())
    # prefetch slack may fetch a few extra row descendants, but never more than
    # the serial walk's total node universe
    assert f_par.calls <= f_serial.calls + 4


def test_row_name_from_cache_no_extra_fetches():
    row = FakeEl("AXRow", children=[FakeEl("AXStaticText", value="Inbox — 3 unread")])
    win = FakeEl("AXWindow", "W", children=[row])
    f = Fetcher()
    with _patched(f):
        text = _snapshot_tree(_session(8), win)
    assert "Inbox — 3 unread" in text          # row got its accessible name
    assert f.calls <= 3                         # win + row + text child, once each


def test_thread_failure_falls_back_serial():
    f = Fetcher(fail_off_main=True)
    with _patched(f):
        text = _snapshot_tree(_session(8), _big_tree())
    assert "Btn2-3" in text                     # complete tree despite thread failure


def test_scoped_snapshot_same_refs_and_occ():
    # three same-named sibling groups exercise the #occ disambiguator
    win = FakeEl("AXWindow", "W")
    for _ in range(3):
        win.children.append(FakeEl("AXGroup", children=[FakeEl("AXButton", "Go")]))
    s = _session(0)
    with _patched(Fetcher()):
        full = _snapshot_tree(s, win)
    # the third "Go" button's ref, from the full walk
    refs = [ln.split("]")[0].strip("[ ") for ln in full.splitlines() if '"Go"' in ln]
    assert len(refs) == 3 and len(set(refs)) == 3
    third_btn_ref = refs[2]
    # AXGroup is scaffolding (never emitted/registered) — scope from the button itself
    before = dict(s.registry)
    with _patched(Fetcher()):
        text, info = s._snapshot_scoped(third_btn_ref, None, None, True)
    assert f"[{third_btn_ref}]" in text          # SAME ref as the full walk (root_key seed)
    for r, el in before.items():                 # every pre-existing ref still maps to its element
        assert s.registry.get(r) is el, r
    assert info["refs"] == len(before)           # subtree re-walk added no new refs here


def test_scoped_unknown_ref_message():
    s = _session(0)
    text, info = s._snapshot_scoped("e99", None, None, True)
    assert "unknown or stale" in text


def test_depth_marker():
    win = FakeEl("AXWindow", "W",
                 children=[FakeEl("AXGroup", children=[FakeEl("AXButton", "Deep")])])
    with _patched(Fetcher()):
        text = _snapshot_tree(_session(0), win, max_depth=0)
    assert "children not walked" in text
    assert "Deep" not in text


def test_sibling_marker():
    win = FakeEl("AXWindow", "W",
                 children=[FakeEl("AXButton", f"B{i}") for i in range(205)])
    with _patched(Fetcher()):
        text = _snapshot_tree(_session(0), win)
    assert "…(+5 more siblings not shown)" in text


def test_budget_footer_and_absence():
    win = FakeEl("AXWindow", "W",
                 children=[FakeEl("AXButton", f"B{i}") for i in range(30)])
    with _patched(Fetcher()):
        text = _snapshot_tree(_session(0), win, max_nodes=10)
        small = _snapshot_tree(_session(0), win)
    assert "…tree truncated at 10 nodes" in text
    assert "truncated" not in small and "not shown" not in small and "not walked" not in small


def test_find_semantics():
    win = _big_tree()
    s = _session(0)
    s._resolve_window = lambda app_name: (win, "FakeApp", None)
    with _patched(Fetcher()):
        text, info = s.find(role="button", name_contains="btn1")
    assert info["matches"] == 4                  # Btn1-0..Btn1-3
    assert "[e" in text and "›" in text          # refs + breadcrumbs
    ref = text.splitlines()[1].split("[")[1].split("]")[0]
    assert s._el(ref) is not None                # find refs resolve for act()
    with _patched(Fetcher()):
        t2, i2 = s.find(role="AXButton", max_results=2)
    assert i2["matches"] == 2 and "stopped at 2 matches" in t2
    with _patched(Fetcher()):
        t3, i3 = s.find(name_contains="no-such-thing")
    assert i3["matches"] == 0 and "no matches" in t3


def test_cap_semantics():
    assert _cap_depth(None, 18) == 18
    assert _cap_depth(-1, 18) == 18
    assert _cap_depth(0, 18) == 0
    assert _cap_nodes(None, 3000) == 3000
    assert _cap_nodes(0, 3000) == 3000
    assert _cap_nodes(-5, 3000) == 3000
    assert _cap_nodes(999999, 3000) == 50000


def test_cdp_truncation_marker():
    from hunch.cdp import CDPSession
    s = CDPSession.__new__(CDPSession)
    s.registry, s._counter, s.snapshot_count = {}, 0, 0
    nodes = {i: {"nodeId": i, "role": {"value": "button"}, "name": {"value": f"b{i}"},
                 "backendDOMNodeId": i, "childIds": []} for i in range(5)}
    lines = []
    budget = {"left": 2, "skipped": 0}
    for n in nodes.values():
        s._walk(n, nodes, 0, lines, True, budget)
    assert len(lines) == 2 and budget["skipped"] == 3


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        getattr(mod, name)()
        print(f"ok {name}")
    print("all walk tests passed")
