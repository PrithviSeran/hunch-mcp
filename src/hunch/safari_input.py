"""Window-routed Safari input. Never posts to the shared HID/session event tap.

WindowServer focus records adapted from background-computer-use (MIT),
Copyright (c) 2026 Anupam Batra. See assets/background-input-LICENSE.txt.
Private symbols are optional: unavailable/ambiguous targets fail closed.
"""
from __future__ import annotations

import ctypes
import json
import math
import subprocess
import sys
import time

from .gate import HunchError


def _window_for_url(url):
    # Safari extension IDs and AppleScript window IDs are different namespaces.
    # An exact, unique URL is the join; never use Safari's front window implicitly.
    script = '''tell application "Safari"
set matches to 0
set targetWindow to 0
repeat with w in windows
repeat with t in tabs of w
if URL of t is %s then
set matches to matches + 1
if URL of current tab of w is %s then set targetWindow to id of w
end if
end repeat
end repeat
if matches is not 1 or targetWindow is 0 then error "Target must be a unique URL in a selected Safari tab"
return targetWindow
end tell''' % (json.dumps(url), json.dumps(url))
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True,
                            text=True, timeout=5)
    if result.returncode:
        raise HunchError('Background input requires one uniquely matching, selected Safari tab; '
                         'duplicate URLs or hidden tabs are refused. No input was sent.')
    return int(result.stdout.strip())


def focus_records(window):
    records = []
    for phase in (13, 1, 2):
        record = bytearray(256)
        record[4], record[8] = 248, phase
        record[60:64] = int(window).to_bytes(4, 'little')
        if phase == 13:
            record[138] = 1
        else:
            record[58] = 16
            record[32:48] = b'\xff' * 16
        records.append(record)
    return records


def pixel_point(x, y, capture, bounds):
    width, height = capture.get('pixelWidth', 0), capture.get('pixelHeight', 0)
    if not width or not height or not all(math.isfinite(float(v)) for v in (x, y)):
        raise HunchError('A fresh web_screenshot with valid dimensions is required')
    if not (0 <= x < width and 0 <= y < height):
        raise HunchError('Coordinates are outside the web screenshot')
    return (bounds['x'] + x * bounds['w'] / width,
            bounds['y'] + y * bounds['h'] / height)


class SafariInput:
    def __init__(self, url):
        import Quartz as Q
        import AppKit
        import ApplicationServices as AX
        from .local_mac import _frontmost
        self.Q, self.front = Q, _frontmost
        if not AX.AXIsProcessTrusted():
            raise HunchError('Safari background input needs Accessibility permission for Hunch')
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.apple.Safari')
        if len(apps) != 1:
            raise HunchError('Safari process identity is ambiguous')
        self.pid = apps[0].processIdentifier()
        self.previous = self.front()
        if self.previous[1] is None or self.previous[1] == self.pid:
            raise HunchError('Background canvas input requires another app in front; '
                             'it will not compete with your active Safari keyboard')
        self.url, self.window = url, _window_for_url(url)
        entries = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionIncludingWindow, self.window)
        if len(entries) != 1 or entries[0].get('kCGWindowOwnerPID') != self.pid:
            raise HunchError('Safari window identity could not be verified')
        self.bounds = entries[0]['kCGWindowBounds']
        ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon', mode=ctypes.RTLD_GLOBAL)
        ctypes.CDLL('/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight', mode=ctypes.RTLD_GLOBAL)
        self.symbols = ctypes.CDLL(None)
        self.post_record = self._symbol('SLPSPostEventRecordTo', [ctypes.c_void_p, ctypes.c_void_p])
        self.psn = (ctypes.c_uint32 * 2)()
        main = self._symbol('CGSMainConnectionID', [])
        owner_fn = self._symbol('CGSGetWindowOwner', [ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p])
        psn_fn = self._symbol('CGSGetConnectionPSN', [ctypes.c_int32, ctypes.c_void_p])
        owner = ctypes.c_int32()
        if owner_fn(main(), self.window, ctypes.byref(owner)) or psn_fn(owner.value, self.psn):
            raise HunchError('Cannot resolve Safari window-owner routing')
        self.source = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)

    def _symbol(self, name, args, result=ctypes.c_int32):
        try:
            fn = getattr(self.symbols, name)
        except AttributeError as exc:
            raise HunchError(f'Background input is unavailable: missing {name}') from exc
        fn.argtypes, fn.restype = args, result
        return fn

    def guard(self, target=False):
        if self.front() != self.previous:
            raise HunchError('Foreground app changed; stopped background input. Do not replay blindly.')
        if target and _window_for_url(self.url) != self.window:
            raise HunchError('Safari target changed; stopped background input')
        if target:
            entries = self.Q.CGWindowListCopyWindowInfo(self.Q.kCGWindowListOptionIncludingWindow, self.window)
            if len(entries) != 1 or entries[0].get('kCGWindowOwnerPID') != self.pid:
                raise HunchError('Safari window ownership changed; stopped background input')
            if dict(entries[0]['kCGWindowBounds']) != dict(self.bounds):
                raise HunchError('Safari window moved/resized; take a fresh screenshot before continuing')

    def prepare(self):
        self.guard(target=True)
        for record in focus_records(self.window):
            buf = (ctypes.c_ubyte * len(record)).from_buffer(record)
            if self.post_record(self.psn, buf):
                raise HunchError('Safari rejected window-specific input preparation')
        time.sleep(.05)
        self.guard()

    def screenshot(self):
        self.guard(target=True)
        try:
            result = subprocess.run([sys.executable, '-m', 'hunch.safari_capture',
                                     str(self.window), str(self.pid)],
                                    capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired as exc:
            raise HunchError('Safari capture worker timed out after 25 seconds; '
                             'no fresh screenshot is available') from exc
        try:
            capture = json.loads(result.stdout)
        except ValueError as exc:
            raise HunchError(f'Safari capture worker returned invalid output '
                             f'(exit {result.returncode}); no fresh screenshot is available. '
                             'This does not establish a Screen Recording permission denial.') from exc
        if not isinstance(capture, dict):
            raise HunchError('Safari capture worker returned an invalid response; '
                             'no fresh screenshot is available')
        if result.returncode or capture.get('error'):
            raise HunchError(capture.get('error') or
                             f'Safari capture worker failed (exit {result.returncode})')
        if (not capture.get('data') or not isinstance(capture.get('pixelWidth'), int)
                or not isinstance(capture.get('pixelHeight'), int)
                or capture['pixelWidth'] <= 0 or capture['pixelHeight'] <= 0):
            raise HunchError('Safari capture worker returned an empty or invalid image')
        if capture.get('nativeBounds') != dict(self.bounds):
            raise HunchError('Safari window moved during capture; take a new screenshot')
        self.guard(target=True)
        return {**capture, 'nativeWindow':True, 'nativeWindowId':self.window}

    def type(self, text):
        if not isinstance(text, str):
            raise HunchError('Background text must be a string')
        if any(ord(char) > 0xffff or 0xd800 <= ord(char) <= 0xdfff for char in text):
            raise HunchError('Supplementary Unicode/emoji is not yet supported by Safari native text input; '
                             'refused before typing rather than silently dropping characters')
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        self.prepare()
        for index, char in enumerate(text):
            # Check user focus each character; periodically revalidate tab identity.
            self.guard(target=index % 32 == 0)
            if char in '\n\r\t':
                self._key(48 if char == '\t' else 36, 0)
                continue
            for down in (True, False):
                ev = self.Q.CGEventCreateKeyboardEvent(self.source, 0, down)
                self.Q.CGEventSetFlags(ev, 0)
                self.Q.CGEventKeyboardSetUnicodeString(ev, len(char.encode('utf-16-le')) // 2, char)
                self.Q.CGEventPostToPid(self.pid, ev)
            time.sleep(.01)
        self.guard(target=True)

    def _key(self, code, flags):
        for down in (True, False):
            ev = self.Q.CGEventCreateKeyboardEvent(self.source, code, down)
            self.Q.CGEventSetFlags(ev, flags)
            self.Q.CGEventPostToPid(self.pid, ev)
        time.sleep(.025)

    def key(self, key, modifiers=()):
        from .local_mac import _KEYCODES, _MODFLAGS
        codes = {**_KEYCODES, 'arrowleft':123, 'arrowright':124, 'arrowdown':125,
                 'arrowup':126, 'home':115, 'end':119, 'pageup':116, 'pagedown':121,
                 'forwarddelete':117}
        parts = key.lower().split('+')
        modifiers = [*modifiers, *parts[:-1]]
        aliases = {'meta':'command', 'alt':'option'}
        modifiers = [aliases.get(m.lower(), m.lower()) for m in modifiers]
        if parts[-1] not in codes or any(m not in _MODFLAGS for m in modifiers):
            raise HunchError('Unsupported background key or modifier')
        self.prepare()
        flags = 0
        for modifier in modifiers:
            flags |= _MODFLAGS[modifier]
        self._key(codes[parts[-1]], flags)
        self.guard()

    def pointer(self, action, capture):
        import objc
        class Point(ctypes.Structure):
            _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]
        main = self._symbol('CGSMainConnectionID', [])
        owner_fn = self._symbol('CGSGetWindowOwner', [ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p])
        owner = ctypes.c_int32()
        if owner_fn(main(), self.window, ctypes.byref(owner)):
            raise HunchError('Cannot resolve Safari mouse routing')
        location = self._symbol('CGEventSetWindowLocation', [ctypes.c_void_p, Point], None)
        send = self._symbol('SLEventPostToPid', [ctypes.c_int, ctypes.c_void_p], None)
        if not capture.get('nativeWindow'):
            raise HunchError('Coordinate input requires a native Safari window screenshot')
        if capture.get('nativeWindowId') != self.window or capture.get('nativeBounds') != dict(self.bounds):
            raise HunchError('Safari window moved/resized since capture; take a fresh web_screenshot')
        viewport = {'x':self.bounds['X'], 'y':self.bounds['Y'],
                    'w':self.bounds['Width'], 'h':self.bounds['Height']}
        dragging = action['action'] == 'drag'
        start = pixel_point(action['from_x'] if dragging else action['x'],
                            action['from_y'] if dragging else action['y'], capture, viewport)
        end = pixel_point(action['to_x'], action['to_y'], capture, viewport) if dragging else start
        self.prepare()
        def post(kind, point):
            ev = self.Q.CGEventCreateMouseEvent(self.source, kind, point, self.Q.kCGMouseButtonLeft)
            self.Q.CGEventSetFlags(ev, 0)
            for field, value in ((40,self.pid),(51,self.window),(52,owner.value),
                                 (85,owner.value),(91,self.window),(92,self.window),(7,3),(1,1)):
                self.Q.CGEventSetIntegerValueField(ev, field, value)
            location(objc.pyobjc_id(ev), Point(point[0]-self.bounds['X'], point[1]-self.bounds['Y']))
            send(self.pid, objc.pyobjc_id(ev))
            time.sleep(.025)
        post(self.Q.kCGEventLeftMouseDown, start)
        try:
            if action['action'] == 'drag':
                for i in range(1, 13):
                    self.guard()
                    post(self.Q.kCGEventLeftMouseDragged,
                         (start[0]+(end[0]-start[0])*i/12, start[1]+(end[1]-start[1])*i/12))
        finally:
            post(self.Q.kCGEventLeftMouseUp, end)
        self.guard(target=True)
