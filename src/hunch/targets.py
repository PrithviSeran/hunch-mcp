"""Session target identity and selection, independent of AX transport calls."""
import os
import plistlib
from dataclasses import dataclass
from functools import lru_cache

from .errors import HunchError


class TargetError(HunchError):
    pass


def normalize_name(value):
    for cp in (0x200E, 0x200F, 0x200B, 0x200C, 0x200D, 0xFEFF,
               0x202A, 0x202B, 0x202C, 0x202D, 0x202E):
        value = value.replace(chr(cp), "")
    return value.strip().casefold()


def select_running(query, candidates):
    """Resolve exactly one process; even exact bundle/path matches may be ambiguous."""
    query = str(query)
    if query.startswith("pid:"):
        matches = [a for a in candidates if str(a["pid"]) == query[4:]]
    elif query.startswith(("/", "~")):
        path = os.path.realpath(os.path.expanduser(query))
        matches = [a for a in candidates if a.get("path") and os.path.realpath(a["path"]) == path]
    else:
        matches = [a for a in candidates if a.get("bundle_id") == query]
        if not matches:
            name = normalize_name(query)
            matches = [a for a in candidates if normalize_name(a.get("name", "")) == name]
            if not matches and name:
                matches = [a for a in candidates if name in normalize_name(a.get("name", ""))]
    if len(matches) > 1:
        choices = "; ".join(f"pid:{a['pid']} {a.get('path') or a.get('name')}" for a in matches)
        raise TargetError(f"ambiguous app {query!r}; select an exact process: {choices}")
    return matches[0] if matches else None


def process_key(identity):
    return (identity.get("pid"), identity.get("started_at"), identity.get("path"),
            identity.get("bundle_id"), identity.get("version"), identity.get("build"))


@lru_cache(maxsize=128)
def _bundle_metadata(path, modified):
    with open(path, "rb") as source:
        info = plistlib.load(source)
    return {"version": str(info.get("CFBundleShortVersionString", "")),
            "build": str(info.get("CFBundleVersion", ""))}


def bundle_metadata(app_path):
    path = os.path.join(app_path, "Contents", "Info.plist")
    try:
        return _bundle_metadata(path, os.stat(path).st_mtime_ns)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def installed_apps(query="", roots=None, limit=500):
    """Bounded bundle inventory, kept separate from running-process identity."""
    roots = roots or ("/Applications", "/System/Applications", os.path.expanduser("~/Applications"))
    entries, seen = [], set()
    query = normalize_name(query)
    for root in roots:
        for directory, children, _ in os.walk(root, followlinks=False):
            relative = os.path.relpath(directory, root)
            depth = 0 if relative == "." else len(relative.split(os.sep))
            bundles = sorted(name for name in children if name.endswith(".app"))
            children[:] = sorted(name for name in children if not name.endswith(".app")) if depth < 3 else []
            for name in bundles:
                path = os.path.realpath(os.path.join(directory, name))
                if path in seen:
                    continue
                seen.add(path)
                try:
                    with open(os.path.join(path, "Contents", "Info.plist"), "rb") as source:
                        info = plistlib.load(source)
                    entry = {"name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or name[:-4]),
                             "path": path, "bundle_id": str(info.get("CFBundleIdentifier", "")),
                             **bundle_metadata(path)}
                except (OSError, ValueError, plistlib.InvalidFileException):
                    continue
                if not query or any(query in normalize_name(entry[key]) for key in ("name", "path", "bundle_id")):
                    entries.append(entry)
                if len(seen) >= limit:
                    return {"installed": entries, "bounded": True, "truncated": True, "scanned": len(seen)}
    return {"installed": entries, "bounded": True, "truncated": False, "scanned": len(seen)}


@dataclass
class WindowTarget:
    handle: str
    element: object
    provenance: list
    generation: int


class TargetRegistry:
    """Opaque window handles last only for the lifetime of their process identity."""
    def __init__(self):
        self.identity = None
        self.windows = []
        self.generation = 0
        self._next = 0

    def bind(self, identity, observations):
        if self.identity is None or process_key(self.identity) != process_key(identity):
            self.windows = []
            self.generation += 1
        self.identity = dict(identity)
        current = []
        for element, provenance in observations[:64]:
            target = next((w for w in self.windows if w.element == element), None)
            if target is None:
                self._next += 1
                target = WindowTarget(f"w{self._next}", element, provenance, self.generation)
            target.provenance = provenance
            current.append(target)
        self.windows = current
        return current

    def window(self, handle):
        match = next((w for w in self.windows if w.handle == handle), None)
        if match is None:
            raise TargetError(f"window {handle!r} is unavailable or stale; inspect targets again")
        return match
