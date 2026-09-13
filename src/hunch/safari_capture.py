"""Isolated ScreenCaptureKit capture worker (Cocoa callbacks run on its main thread).

Legacy CGWindow/screencapture and Safari captureVisibleTab can omit live canvas layers
in an occluded window. Capture only the independently validated Safari window instead.
"""
import base64
import json
import sys
import time
from functools import lru_cache


class CaptureError(RuntimeError):
    def __init__(self, message, *, stage, domain=None, code=None):
        super().__init__(message)
        self.stage, self.domain, self.code = stage, domain, code


def _raise_native(error, stage):
    if error is not None:
        raise CaptureError(
            f'ScreenCaptureKit {error.domain()} code {error.code()}: {error.localizedDescription()}',
            stage=stage, domain=error.domain(), code=error.code())


@lru_cache(maxsize=1)
def _output_class():
    import Foundation
    import CoreMedia
    import Quartz
    import ScreenCaptureKit as SC
    import objc

    class HunchSafariStreamOutput(Foundation.NSObject,
                                 protocols=[objc.protocolNamed('SCStreamOutput'),
                                            objc.protocolNamed('SCStreamDelegate')]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
            if kind != SC.SCStreamOutputTypeScreen or 'image' in self.state:
                return
            try:
                attachments = CoreMedia.CMSampleBufferGetSampleAttachmentsArray(sample, False)
                if (not attachments or attachments[0].get(SC.SCStreamFrameInfoStatus)
                        != SC.SCFrameStatusComplete):
                    return
                buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample)
                if buffer is None:
                    return
                source = Quartz.CIImage.imageWithCVPixelBuffer_(buffer)
                image = self.context.createCGImage_fromRect_(source, source.extent())
                if image is None:
                    raise CaptureError('ScreenCaptureKit frame conversion failed', stage='frame')
                # Convert while the sample buffer is alive; never return a borrowed buffer.
                self.state['image'] = image
            except Exception as exc:
                self.state['callback_error'] = exc

        def stream_didStopWithError_(self, stream, error):
            self.state['stream_error'] = error

    return HunchSafariStreamOutput


def _capture_stream(content_filter, config, wait, state):
    import Quartz
    import ScreenCaptureKit as SC
    output = _output_class().alloc().init()
    output.state = state
    output.context = Quartz.CIContext.contextWithOptions_(None)
    stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(content_filter, config, output)
    ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
        output, SC.SCStreamOutputTypeScreen, None, None)
    _raise_native(error, 'output')
    if not ok:
        raise CaptureError('ScreenCaptureKit rejected the stream output', stage='output')
    try:
        stream.startCaptureWithCompletionHandler_(lambda error: state.update(start=error))
        wait('start')
        _raise_native(state['start'], 'start')
        wait('image')
        return state['image']
    finally:
        # Stop on success, timeout, conversion failure, or asynchronous stream error.
        # The parent also bounds this isolated worker's lifetime.
        failed = sys.exc_info()[0] is not None
        try:
            stream.stopCaptureWithCompletionHandler_(lambda error: state.update(stop=error))
            wait('stop', cleanup=True)
            _raise_native(state['stop'], 'stop')
        except Exception:
            if not failed:
                raise


def capture(window_id, pid):
    # Quartz must register CGImage with PyObjC BEFORE the ScreenCaptureKit callback.
    import Quartz
    import ScreenCaptureKit as SC
    import Foundation
    import AppKit
    AppKit.NSApplication.sharedApplication()
    state = {}
    def wait(key, cleanup=False):
        deadline = time.monotonic() + 5
        while key not in state and time.monotonic() < deadline:
            if not cleanup:
                if 'callback_error' in state:
                    raise state['callback_error']
                _raise_native(state.get('stream_error'), key)
            Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(.02))
        if key not in state:
            raise CaptureError('ScreenCaptureKit timed out', stage=key)
        if not cleanup:
            if 'callback_error' in state:
                raise state['callback_error']
            _raise_native(state.get('stream_error'), key)
            _raise_native(state.get('error'), key)
    def content_done(content, error):
        state.update(content=content, error=error)
    SC.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        True, False, content_done)
    wait('content')
    matches = [w for w in state['content'].windows() if w.windowID() == window_id
               and w.owningApplication() is not None and w.owningApplication().processID() == pid]
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
    # The one-shot screenshot API fails with -3811 for Safari in another full-screen
    # Space. A short desktop-independent stream delivers its complete canvas frame.
    image = _capture_stream(content_filter, config, wait, state)
    rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
    if rep.pixelsWide() != int(frame.size.width) or rep.pixelsHigh() != int(frame.size.height):
        raise CaptureError('Safari capture dimensions changed; take a new screenshot', stage='frame')
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
