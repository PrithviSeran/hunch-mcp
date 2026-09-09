"""Exercise SDK reads against changing, fake AX windows without touching the UI."""
import pytest

from hunch import Hunch, local_mac


@pytest.fixture
def bound_safari(monkeypatch):
    safari = {"pid": 42, "name": "Safari", "bundle_id": "com.apple.Safari",
              "path": "/Applications/Safari.app", "started_at": "original"}
    notes = {"pid": 43, "name": "Notes", "bundle_id": "com.apple.Notes",
             "path": "/Applications/Notes.app", "started_at": "original"}
    apps = [safari, notes]
    windows = {42: ["Course", "Personal"], 43: ["Notes window"]}
    focused = {42: "Course", 43: "Notes window"}
    reads = []

    def attrs(element, names):
        reads.append(element)
        values = {"AXRole": "AXWindow", "AXTitle": element, "AXChildren": []}
        return {name: values.get(name) for name in names}

    monkeypatch.setattr(local_mac.ax, "list_apps", lambda: apps)
    monkeypatch.setattr(local_mac, "_running_identity",
                        lambda pid: next((dict(app) for app in apps if app["pid"] == pid), {}))
    monkeypatch.setattr(local_mac, "AXUIElementCreateApplication", lambda pid: pid)
    monkeypatch.setattr(local_mac, "AXIsProcessTrusted", lambda: True)
    monkeypatch.setattr(local_mac, "_embedded_chromium", lambda pid: False)
    monkeypatch.setattr(local_mac, "_twin_process_warning", lambda *args: "")
    monkeypatch.setattr(local_mac.ax, "get_window", lambda pid: focused[pid])
    monkeypatch.setattr(local_mac.ax, "discover_windows",
                        lambda pid: ([(w, ["AXWindows"]) for w in windows[pid]], []))
    monkeypatch.setattr(local_mac.ax, "get_attr",
                        lambda element, name: attrs(element, [name])[name])
    monkeypatch.setattr(local_mac.ax, "get_attrs", attrs)
    monkeypatch.setattr(local_mac.MacSession, "activate",
                        lambda *args: pytest.fail("background read activated an app"))
    monkeypatch.setattr(local_mac, "_frontmost",
                        lambda: pytest.fail("explicit reads followed foreground app"))
    mac = Hunch(check_permissions=False, confirm="off", background_only=True)
    target = mac.targets(app="Safari")
    course = next(w["handle"] for w in target["windows"] if w["title"] == "Course")
    mac.targets(app="pid:42", window=course, select=True)
    reads.clear()
    return mac, focused, windows, apps, reads


@pytest.mark.parametrize("method", ["snapshot", "find"])
@pytest.mark.parametrize("alias", ["", "Safari", "com.apple.Safari",
                                  "/Applications/Safari.app", "pid:42"])
def test_reads_stay_on_selected_window_when_user_switches(bound_safari, method, alias):
    mac, focused, _, _, reads = bound_safari
    read = getattr(mac, method)
    assert "Course" in read()
    focused[42] = "Personal"
    reads.clear()
    result = read(app=alias)
    assert "Course" in result
    assert "Personal" not in result
    assert set(reads) == {"Course"}
    assert "Course" in read()
    assert not any(mac._computer.session.disturbances.values())
    if method == "snapshot":
        assert "selected window" in result


@pytest.mark.parametrize("method", ["snapshot", "find"])
def test_closed_selected_window_refuses_instead_of_following_focus(bound_safari, method):
    mac, focused, windows, _, reads = bound_safari
    read = getattr(mac, method)
    read()
    assert mac._computer.session.registry
    windows[42].remove("Course")
    focused[42] = "Personal"
    reads.clear()
    assert "selected window is stale" in read(app="Safari")
    assert "selected window is stale" in read()
    assert not reads
    assert not mac._computer.session.registry


@pytest.mark.parametrize("method", ["snapshot", "find"])
def test_reused_pid_does_not_rebind_selected_window(bound_safari, method):
    mac, focused, _, apps, reads = bound_safari
    apps[0]["started_at"] = "restarted"
    focused[42] = "Personal"
    assert "selected window is stale" in getattr(mac, method)(app="Safari")
    assert not reads


@pytest.mark.parametrize("method", ["snapshot", "find"])
def test_explicit_other_app_releases_window_binding(bound_safari, method):
    mac, _, _, _, reads = bound_safari
    result = getattr(mac, method)(app="Notes")
    assert "Notes window" in result
    assert set(reads) == {"Notes window"}
    assert mac._computer.session._selected_window is None


@pytest.mark.parametrize("method", ["snapshot", "find"])
@pytest.mark.parametrize("failure", ["missing", "ambiguous"])
def test_failed_app_resolution_preserves_window_binding(bound_safari, method, failure):
    mac, focused, _, apps, reads = bound_safari
    focused[42] = "Personal"
    if failure == "ambiguous":
        apps.append({**apps[0], "pid": 44})
        query = "Safari"
        expected = "ambiguous app"
    else:
        query = "Missing"
        expected = "not found"
    read = getattr(mac, method)
    assert expected in read(app=query)
    assert not reads
    assert "Course" in read(app="pid:42")
