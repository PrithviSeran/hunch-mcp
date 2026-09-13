from types import SimpleNamespace

import pytest

from hunch import safari_capture as capture


@pytest.mark.parametrize('failure', [None, 'start', 'frame', 'stop'])
def test_stream_always_stops_and_propagates_failure(monkeypatch, failure):
    import Quartz
    import ScreenCaptureKit as SC
    state, calls = {}, []
    error = SimpleNamespace(domain=lambda: 'test', code=lambda: 7,
                            localizedDescription=lambda: 'failure')
    class Output:
        @classmethod
        def alloc(cls): return cls()
        def init(self): return self
    class Stream:
        @classmethod
        def alloc(cls): return cls()
        def initWithFilter_configuration_delegate_(self, *args): return self
        def addStreamOutput_type_sampleHandlerQueue_error_(self, *args): return True, None
        def startCaptureWithCompletionHandler_(self, callback):
            calls.append('start')
            callback(error if failure == 'start' else None)
        def stopCaptureWithCompletionHandler_(self, callback):
            calls.append('stop')
            callback(error if failure == 'stop' else None)
    def wait(key, cleanup=False):
        if key == 'image':
            if failure == 'frame':
                raise capture.CaptureError('No complete frame', stage='frame')
            state['image'] = 'frame'
    monkeypatch.setattr(capture, '_output_class', lambda: Output)
    monkeypatch.setattr(SC, 'SCStream', Stream)
    if failure:
        with pytest.raises(capture.CaptureError):
            capture._capture_stream(None, None, wait, state)
    else:
        assert capture._capture_stream(None, None, wait, state) == 'frame'
    assert calls == ['start', 'stop']


@pytest.mark.parametrize('status,has_buffer,expected', [(1,True,False), (2,True,False),
                                                    (0,False,False), (0,True,True)])
def test_only_complete_frames_with_buffers_are_accepted(monkeypatch, status, has_buffer, expected):
    import CoreMedia
    import Quartz
    import ScreenCaptureKit as SC
    output = capture._output_class().alloc().init()
    output.state = {}
    output.context = SimpleNamespace(createCGImage_fromRect_=lambda *args: 'image')
    monkeypatch.setattr(CoreMedia, 'CMSampleBufferGetSampleAttachmentsArray',
                        lambda *args: [{SC.SCStreamFrameInfoStatus: status}])
    monkeypatch.setattr(CoreMedia, 'CMSampleBufferGetImageBuffer',
                        lambda *args: object() if has_buffer else None)
    monkeypatch.setattr(Quartz, 'CIImage', SimpleNamespace(
        imageWithCVPixelBuffer_=lambda _: SimpleNamespace(extent=lambda: None)))
    output.stream_didOutputSampleBuffer_ofType_(None, None, SC.SCStreamOutputTypeScreen)
    assert ('image' in output.state) == expected
    assert 'callback_error' not in output.state
