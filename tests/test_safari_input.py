import pytest
from hunch.gate import HunchError
from hunch.safari_input import focus_records, pixel_point, SafariInput
from hunch.safari import SafariComputer


def test_focus_records_are_target_only_and_window_specific():
    records=focus_records(4238)
    assert [r[8] for r in records]==[13,1,2]
    assert all(int.from_bytes(r[60:64],'little')==4238 for r in records)
    assert records[0][138]==1
    assert all(r[32:48]==b'\xff'*16 for r in records[1:])


def test_retina_coordinate_mapping_and_bounds():
    capture={'pixelWidth':2000,'pixelHeight':1600}
    bounds={'x':-1000,'y':100,'w':1000,'h':800}
    assert pixel_point(1000,800,capture,bounds)==(-500,500)
    for x,y in [(2000,0),(-1,0),(0,1600),(float('nan'),0)]:
        with pytest.raises(HunchError):pixel_point(x,y,capture,bounds)


def test_focus_change_stops_native_input():
    target=SafariInput.__new__(SafariInput)
    target.previous=('Editor',1)
    target.front=lambda:('Safari',2)
    with pytest.raises(HunchError,match='Foreground app changed'):target.guard()


class Bridge:
    def __init__(self):self.calls=[];self.block=False
    def request(self,op,payload):
        self.calls.append((op,payload))
        if self.block:return {'status':'blocked','reason':'stale generation'}
        return {'status':'verified','tabId':1,'windowId':2,'url':'https://example.com/',
                'generation':'g','tree':'canvas','data':'png','pixelWidth':100,'pixelHeight':100}


@pytest.fixture
def bound(monkeypatch):
    calls=[]
    class Native:
        def __init__(self,url):calls.append(('target',url))
        def type(self,text):calls.append(('type',text))
        def key(self,key,mods):calls.append(('key',key,mods))
        def pointer(self,action,capture):calls.append(('pointer',action))
        def screenshot(self):return {'data':'png','pixelWidth':100,'pixelHeight':100}
    monkeypatch.setattr('hunch.safari_input.SafariInput',Native)
    bridge=Bridge();computer=SafariComputer(client=bridge)
    computer._bind(bridge.request('snapshot',{}))
    return computer,bridge,calls


def test_no_ref_type_and_keys_use_native_not_dom(bound):
    computer,bridge,calls=bound
    result=computer.act([{'action':'type','text':'hello'},
                         {'action':'key','key':'a','modifiers':['command']}],detailed=True)
    assert result['status']=='performed_unverified'
    assert result['completedActions']==2
    assert ('type','hello') in calls and ('key','a',['command']) in calls
    assert not any(op=='act' for op,_ in bridge.calls)


def test_coordinate_capture_is_required_and_consumed(bound):
    computer,bridge,calls=bound
    action={'action':'click_xy','x':1,'y':2}
    assert computer.act([action],detailed=True)['status']=='blocked'
    computer.capture_screenshot()
    assert computer.act([action],detailed=True)['status']=='performed_unverified'
    assert computer.act([action],detailed=True)['status']=='blocked'
    assert len([c for c in calls if c[0]=='pointer'])==1


def test_stale_binding_sends_no_native_input(bound):
    computer,bridge,calls=bound;bridge.block=True
    result=computer.act([{'action':'type','text':'no'}],detailed=True)
    assert result['status']=='blocked' and result['completedActions']==0
    assert calls==[]


def test_same_url_reload_stops_before_native_input(bound):
    computer,bridge,calls=bound
    original=bridge.request
    bridge.request=lambda op,payload:{**original(op,payload),'generation':'new-document'}
    result=computer.act([{'action':'type','text':'no'}],detailed=True)
    assert result['status']=='blocked' and 'reloaded' in result['reason']
    assert calls==[]


def test_postcondition_is_not_falsely_verified(bound):
    computer,_,_=bound
    result=computer.act([{'action':'type','text':'yes'}],detailed=True,postcondition={'text':'yes'})
    assert result['status']=='performed_unverified'
    assert 'not been evaluated' in result['summary']


def test_unicode_events_only_go_to_target_pid(monkeypatch):
    class Quartz:
        def __init__(self):self.sent=[];self.unicode=[]
        def CGEventCreateKeyboardEvent(self,source,code,down):return {'down':down}
        def CGEventSetFlags(self,event,flags):assert flags==0
        def CGEventKeyboardSetUnicodeString(self,event,length,text):self.unicode.append((length,text))
        def CGEventPostToPid(self,pid,event):self.sent.append((pid,event))
    target=SafariInput.__new__(SafariInput)
    target.Q=Quartz();target.pid=123;target.source=object()
    target.prepare=lambda:None;target.guard=lambda **kw:None
    monkeypatch.setattr('hunch.safari_input.time.sleep',lambda _:None)
    target.type('A✓')
    assert len(target.Q.sent)==4 and all(pid==123 for pid,_ in target.Q.sent)
    assert target.Q.unicode==[(1,'A'),(1,'A'),(1,'✓'),(1,'✓')]


def test_unsupported_unicode_is_rejected_before_any_input():
    target=SafariInput.__new__(SafariInput)
    target.prepare=lambda:pytest.fail('must reject before preparing or typing')
    with pytest.raises(HunchError,match='before typing'):target.type('prefix 😀 suffix')


def test_capture_uses_isolated_screen_capture_kit_worker(monkeypatch):
    target=SafariInput.__new__(SafariInput)
    target.bounds={'X':688,'Y':54,'Width':1232,'Height':950}
    target.window=42;target.pid=123;target.guard=lambda **kw:None
    import json
    import subprocess
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,0,json.dumps({'data':'png','pixelWidth':1232,
            'pixelHeight':950,'nativeBounds':target.bounds}), '')
    monkeypatch.setattr('hunch.safari_input.subprocess.run',run)
    result=target.screenshot()
    assert calls[0][-4:]==['-m','hunch.safari_capture','42','123']
    assert result['nativeWindowId']==42 and result['nativeWindow'] is True


@pytest.mark.parametrize('failure', ['bridge', 'native'])
def test_failed_capture_refresh_invalidates_previous_coordinates(bound, monkeypatch, failure):
    computer, bridge, calls = bound
    computer.capture_screenshot()
    if failure == 'bridge':
        bridge.block = True
    else:
        def fail(self):
            raise HunchError('capture failed')
        monkeypatch.setattr('hunch.safari_input.SafariInput.screenshot', fail)
    with pytest.raises(HunchError):
        computer.capture_screenshot()
    bridge.block = False
    result = computer.act([{'action':'click_xy','x':1,'y':2}], detailed=True)
    assert result['status'] == 'blocked'
    assert not any(c[0] == 'pointer' for c in calls)


@pytest.fixture
def capture_target(monkeypatch):
    target = SafariInput.__new__(SafariInput)
    target.bounds = {'X':0,'Y':0,'Width':100,'Height':100}
    target.window, target.pid = 42, 123
    target.guard = lambda **kw: None
    monkeypatch.setattr('hunch.safari_input.time.sleep', lambda _: None)
    return target


@pytest.mark.parametrize('code,stage,expected_calls', [(-3811,'image',2), (-3801,'image',1),
                                                     (-3811,'content',1)])
def test_capture_retries_only_capture_start_failure(capture_target, monkeypatch, code, stage, expected_calls):
    import json
    import subprocess
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            response = {'error':'capture failed', 'stage':stage, 'code':code,
                        'domain':'com.apple.ScreenCaptureKit.SCStreamErrorDomain'}
            return subprocess.CompletedProcess(argv, 1, json.dumps(response), '')
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            'data':'png','pixelWidth':100,'pixelHeight':100,'nativeBounds':capture_target.bounds}), '')
    monkeypatch.setattr('hunch.safari_input.subprocess.run', run)
    if expected_calls == 2:
        assert capture_target.screenshot()['data'] == 'png'
    else:
        with pytest.raises(HunchError, match='capture failed'):
            capture_target.screenshot()
    assert len(calls) == expected_calls


def test_capture_retry_stops_when_target_changes(capture_target, monkeypatch):
    import json
    import subprocess
    calls = []
    def guard(**kw):
        if calls:
            raise HunchError('Safari target changed')
    capture_target.guard = guard
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, json.dumps({
            'error':'capture failed','stage':'image','code':-3811,
            'domain':'com.apple.ScreenCaptureKit.SCStreamErrorDomain'}), '')
    monkeypatch.setattr('hunch.safari_input.subprocess.run', run)
    with pytest.raises(HunchError, match='target changed'):
        capture_target.screenshot()
    assert len(calls) == 1


def test_worker_invalid_output_does_not_claim_permission_denial(capture_target, monkeypatch):
    import subprocess
    monkeypatch.setattr('hunch.safari_input.subprocess.run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, '', ''))
    with pytest.raises(HunchError, match='does not establish a Screen Recording permission denial'):
        capture_target.screenshot()


def test_persistent_capture_start_failure_stops_after_one_retry(capture_target, monkeypatch):
    import json
    import subprocess
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, json.dumps({
            'error':'ScreenCaptureKit code -3811','stage':'image','code':-3811,
            'domain':'com.apple.ScreenCaptureKit.SCStreamErrorDomain'}), '')
    monkeypatch.setattr('hunch.safari_input.subprocess.run', run)
    with pytest.raises(HunchError, match='-3811'):
        capture_target.screenshot()
    assert len(calls) == 2


def test_capture_worker_timeout_is_a_capture_error(capture_target, monkeypatch):
    import subprocess
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 25)
    monkeypatch.setattr('hunch.safari_input.subprocess.run', run)
    with pytest.raises(HunchError, match='timed out after 25 seconds'):
        capture_target.screenshot()
