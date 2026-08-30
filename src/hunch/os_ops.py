"""os_ops.py — focus-free OS-level primitives via macOS frameworks.

Many operations don't need UI automation at all — the OS exposes them directly, and
going through the API is BOTH focus-free AND more reliable than driving Finder/menus.
This is the "OS-API layer" that complements the AX (accessibility) and CDP backends.

Everything here runs without touching the user's cursor, keyboard, or foreground window.
"""
import os
import subprocess
import tempfile

from Foundation import NSURL, NSFileManager
from AppKit import NSWorkspace, NSPasteboard, NSPasteboardTypeString


def _abspath(p):
    return os.path.abspath(os.path.expanduser(str(p)))


def _url(p):
    return NSURL.fileURLWithPath_(_abspath(p))


# ── Filesystem (focus-free; trash is reversible — files go to the Trash, not gone) ──
def trash(paths):
    """Move file(s)/folder(s) to the Trash by path — focus-free, reversible. The right way
    to 'delete' via Hunch, instead of selecting in Finder and pressing ⌘⌫."""
    fm = NSFileManager.defaultManager()
    out = []
    for p in (paths if isinstance(paths, list) else [paths]):
        full = _abspath(p)
        if not os.path.exists(full):
            out.append(f"not found: {p}")
            continue
        ok, _, err = fm.trashItemAtURL_resultingItemURL_error_(_url(full), None, None)
        out.append(f"trashed: {full}" if ok else f"FAILED: {full} ({err})")
    return "\n".join(out) or "no paths given"


def move(src, dst):
    """Move/rename a file or folder (dst may be a new path or an existing directory)."""
    s, d = _abspath(src), _abspath(dst)
    if os.path.isdir(d):
        d = os.path.join(d, os.path.basename(s))
    ok, err = NSFileManager.defaultManager().moveItemAtURL_toURL_error_(_url(s), _url(d), None)
    return f"moved {s} -> {d}" if ok else f"FAILED: {err}"


def copy(src, dst):
    """Copy a file or folder."""
    s, d = _abspath(src), _abspath(dst)
    if os.path.isdir(d):
        d = os.path.join(d, os.path.basename(s))
    ok, err = NSFileManager.defaultManager().copyItemAtURL_toURL_error_(_url(s), _url(d), None)
    return f"copied {s} -> {d}" if ok else f"FAILED: {err}"


def make_dir(path):
    """Create a folder (including intermediate dirs)."""
    ok, err = NSFileManager.defaultManager().createDirectoryAtURL_withIntermediateDirectories_attributes_error_(
        _url(path), True, None, None)
    return f"created {_abspath(path)}" if ok else f"FAILED: {err}"


def reveal(paths):
    """Reveal/select item(s) in a Finder window (does front Finder — it's a 'show me' action)."""
    urls = [_url(p) for p in (paths if isinstance(paths, list) else [paths])]
    NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(urls)
    return f"revealed {len(urls)} item(s) in Finder"


def open_path(path, app=None):
    """Open a file/folder/URL with its default app (or a named app) — focus-free launch."""
    ws = NSWorkspace.sharedWorkspace()
    target = _abspath(path) if os.path.exists(_abspath(path)) else str(path)
    if app:
        subprocess.run(["open", "-a", app, target], check=False)
        return f"opened {target} with {app}"
    if os.path.exists(target):
        ok = ws.openURL_(NSURL.fileURLWithPath_(target))
    else:
        ok = ws.openURL_(NSURL.URLWithString_(target))  # web URL / scheme
    return f"opened {target}" if ok else f"FAILED to open {target}"


# ── Clipboard (focus-free; replaces ⌘C / ⌘V keystrokes) ──
def clipboard_read():
    """Read the clipboard's text."""
    return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or ""


def clipboard_write(text):
    """Put text on the clipboard (so the user or another app can paste it)."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(str(text), NSPasteboardTypeString)
    return f"copied {len(str(text))} chars to the clipboard"


# ── Apple Events / AppleScript (focus-free control of scriptable native apps) ──
def run_applescript(script, timeout=60):
    """Execute AppleScript via osascript (Apple Events → the app's command layer, not its UI).
    Returns (ok, output_or_error). Focus-free for scriptable apps."""
    fd, path = tempfile.mkstemp(suffix=".scpt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        r = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if r.returncode != 0:
        return False, (r.stderr or "").strip()
    return True, (r.stdout or "").strip()
