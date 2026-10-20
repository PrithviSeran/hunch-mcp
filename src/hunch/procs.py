"""Process facts the CDP endpoint check needs.

macOS keeps the existing NSRunningApplication / lsof / NSWorkspace behavior.
Linux reads /proc so the web surface can attach, launch, and quit Chrome
without pyobjc. Pass proc_root to the linux_* functions to test against a
fake tree.
"""
import os
import shutil
import signal
import subprocess
import sys
import time


_LINUX_BROWSERS = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")


def _parse_stat(text):
    """Fields that survive a comm name containing spaces or parentheses."""
    end = text.rfind(")")
    start = text.find("(")
    if start < 0 or end < start:
        return None
    rest = text[end + 1:].split()
    # state, ppid, then fields 5..21, then starttime (field 22).
    if len(rest) < 20:
        return None
    try:
        return {"comm": text[start + 1:end], "state": rest[0], "ppid": int(rest[1]),
                "starttime": rest[19]}
    except ValueError:
        return None


def _read_stat(proc_root, pid):
    try:
        with open(os.path.join(proc_root, str(pid), "stat")) as source:
            return _parse_stat(source.read())
    except OSError:
        return None


def _boot_id(proc_root):
    try:
        with open(os.path.join(proc_root, "sys", "kernel", "random", "boot_id")) as source:
            return source.read().strip()
    except OSError:
        return ""


def linux_running_identity(pid, proc_root="/proc"):
    """pid, boot-stable start time, and the real executable path. No bundle metadata."""
    parsed = _read_stat(proc_root, pid)
    if not parsed or parsed["state"] == "Z":
        return {}
    exe = os.path.join(proc_root, str(pid), "exe")
    try:
        target = os.readlink(exe)
    except OSError:
        return {}
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(exe), target)
    return {"pid": int(pid), "started_at": f"{_boot_id(proc_root)}:{parsed['starttime']}",
            "bundle_id": "", "path": os.path.realpath(target), "name": parsed["comm"],
            "version": "", "build": ""}


def _listen_inodes(proc_root, port):
    inodes = set()
    for name in ("net/tcp", "net/tcp6"):
        try:
            with open(os.path.join(proc_root, name)) as source:
                lines = source.read().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            try:
                local_port = int(parts[1].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                continue
            if local_port == int(port) and parts[9] != "0":
                inodes.add(parts[9])
    return inodes


def linux_endpoint_listeners(port, proc_root="/proc"):
    """LISTEN sockets on port, then the pids holding those socket inodes."""
    inodes = _listen_inodes(proc_root, port)
    if not inodes:
        return {}
    listeners = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return {}
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = os.path.join(proc_root, entry, "fd")
        sockets = set()
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inode = target[len("socket:["):-1]
                if inode in inodes:
                    sockets.add(inode)
        if not sockets:
            continue
        parsed = _read_stat(proc_root, pid)
        listeners[pid] = {"parent": parsed["ppid"] if parsed else None, "sockets": sockets}
    return listeners


def linux_proc_cmdline(pid, proc_root="/proc"):
    try:
        with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as source:
            raw = source.read()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def linux_process_alive(pid, proc_root="/proc"):
    """Kernel liveness. A zombie is dead. A fake proc_root trusts the stat file."""
    parsed = _read_stat(proc_root, pid)
    if not parsed or parsed["state"] == "Z":
        return False
    if os.path.realpath(proc_root) != "/proc":
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _executable(path):
    """realpath, then the sibling `chrome` ELF when the entry is a wrapper script.

    Google's .deb exposes google-chrome-stable as a script that execs `chrome` beside
    it. /proc/<pid>/exe is that ELF, so the path we launch has to be the ELF too.
    """
    path = os.path.realpath(path)
    try:
        with open(path, "rb") as source:
            elf = source.read(4) == b"\x7fELF"
    except OSError:
        elf = False
    if elf:
        return path
    sibling = os.path.join(os.path.dirname(path), "chrome")
    if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
        try:
            with open(sibling, "rb") as source:
                if source.read(4) == b"\x7fELF":
                    return os.path.realpath(sibling)
        except OSError:
            pass
    return path


def linux_app_path(app):
    """Absolute path as given, else HUNCH_CHROME, else a Chrome binary on PATH."""
    if app and os.path.isabs(app):
        return _executable(app)
    configured = os.environ.get("HUNCH_CHROME")
    if configured:
        if not os.path.isabs(configured):
            configured = shutil.which(configured)
        if not configured:
            raise RuntimeError(f"HUNCH_CHROME={os.environ.get('HUNCH_CHROME')!r} was not found")
        return _executable(configured)
    for name in _LINUX_BROWSERS:
        found = shutil.which(name)
        if found:
            return _executable(found)
    raise RuntimeError("cannot resolve a Chrome binary; set HUNCH_CHROME or install "
                       "google-chrome-stable")


def darwin_endpoint_listeners(port):
    result = subprocess.run(["lsof", "-nP", "-FpdRn", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=3)
    listeners = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            current = listeners.setdefault(int(line[1:]), {"parent": None, "sockets": set()})
        elif current is not None and line.startswith("R"):
            current["parent"] = int(line[1:])
        elif current is not None and line.startswith("d"):
            current["sockets"].add(line[1:])
    if result.returncode:
        return {}
    return listeners


def darwin_app_path(app):
    from AppKit import NSWorkspace
    if os.path.isabs(app):
        return os.path.realpath(app)
    path = NSWorkspace.sharedWorkspace().fullPathForApplication_(app)
    if not path:
        raise RuntimeError(f"cannot resolve installed application {app!r}")
    return os.path.realpath(str(path))


def running_identity(pid):
    if sys.platform != "darwin":
        return linux_running_identity(pid)
    from .local_mac import _running_identity
    return _running_identity(pid)


def endpoint_listeners(port):
    if sys.platform != "darwin":
        return linux_endpoint_listeners(port)
    return darwin_endpoint_listeners(port)


def proc_cmdline(pid):
    if sys.platform != "darwin":
        return linux_proc_cmdline(pid)
    from .local_mac import _proc_cmdline
    return _proc_cmdline(pid)


def process_alive(pid):
    if sys.platform != "darwin":
        return linux_process_alive(pid)
    from .local_mac import _process_alive
    return _process_alive(pid)


def app_path(app):
    if sys.platform != "darwin":
        return linux_app_path(app)
    return darwin_app_path(app)


def terminate(pid):
    """Ask the process to quit, then wait. Never force-kills."""
    pid = int(pid)
    if sys.platform == "darwin":
        from AppKit import NSRunningApplication
        from .local_mac import _process_alive
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            raise RuntimeError("CDP owning process disappeared")
        app.terminate()
        alive = _process_alive
    else:
        if not linux_process_alive(pid):
            raise RuntimeError("CDP owning process disappeared")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            raise RuntimeError("CDP owning process disappeared") from None
        alive = linux_process_alive
    deadline = time.monotonic() + 6
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if alive(pid):
        raise RuntimeError("CDP application did not quit gracefully; resolve its save/confirmation prompt")
