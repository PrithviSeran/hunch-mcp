"""Isolated ScreenCaptureKit capture worker (Cocoa callbacks run on its main thread).

Legacy CGWindow/screencapture and Safari captureVisibleTab can omit live canvas layers
in an occluded window. Capture only the independently validated Safari window instead.
"""
import base64
import json
import sys
import time


class CaptureError(RuntimeError):
    def __init__(self, message, *, stage, domain=None, code=None):
        super().__init__(message)
        self.stage, self.domain, self.code = stage, domain, code


def capture(window_id, pid):
    # Quartz must register CGImage with PyObjC BEFORE the ScreenCaptureKit callback.
    import Quartz
    import ScreenCaptureKit as SC
    import Foundation
    import AppKit
    AppKit.NSApplication.sharedApplication()
    state = {}
    def wait(key):
        deadline = time.monotonic() + 10
        while key not in state and time.monotonic() < deadline:
            Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(.02))
        if key not in state:
            raise CaptureError('ScreenCaptureKit timed out', stage=key)
        if state.get('error') is not None:
            error = state['error']
            raise CaptureError(
                f'ScreenCaptureKit {error.domain()} code {error.code()}: {error.localizedDescription()}',
                stage=key, domain=error.domain(), code=error.code())
    def content_done(content, error):
        state.update(content=content, error=error)
    SC.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        True, False, content_done)
    wait('content')
    matches = [w for w in state['content'].windows() if w.windowID() == window_id
               and w.owningApplication().processID() == pid]
    if len(matches) != 1:
        raise RuntimeError('Bound Safari window is no longer capturable')
    window = matches[0]
    frame = window.frame()
    content_filter = SC.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
    config = SC.SCStreamConfiguration.alloc().init()
    config.setWidth_(int(frame.size.width))
    config.setHeight_(int(frame.size.height))
    config.setShowsCursor_(False)
    config.setIgnoreShadowsSingleWindow_(True)
    config.setCapturesAudio_(False)
    def image_done(image, error):
        state.update(image=image, error=error)
    SC.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
        content_filter, config, image_done)
    wait('image')
    rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(state['image'])
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    return {'data':base64.b64encode(bytes(data)).decode('ascii'),
            'pixelWidth':int(rep.pixelsWide()), 'pixelHeight':int(rep.pixelsHigh()),
            'nativeBounds':{'X':frame.origin.x, 'Y':frame.origin.y,
                            'Width':frame.size.width, 'Height':frame.size.height}}


if __name__ == '__main__':
    try:
        print(json.dumps(capture(int(sys.argv[1]), int(sys.argv[2]))))
    except Exception as exc:
        print(json.dumps({'error':str(exc), 'stage':getattr(exc, 'stage', 'worker'),
                          'domain':getattr(exc, 'domain', None), 'code':getattr(exc, 'code', None)}))
        sys.exit(1)
