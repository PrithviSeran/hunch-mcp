"""Opt-in live test: opens/closes ONLY a unique disposable localhost Safari window.

Run with Accessibility permission: PYTHONPATH=src python tests/manual_safari_input.py
"""
import http.server
import json
import subprocess
import threading
import time
import sys
import Quartz as Q
from hunch.safari_input import SafariInput, _window_for_url
from hunch.local_mac import _frontmost

events = []
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<!doctype html><title>Hunch disposable background input test</title>
<canvas width="700" height="300"></canvas><textarea style="position:absolute;left:-9999px"></textarea>
<script>
const t=document.querySelector('textarea'), c=document.querySelector('canvas');
function paint(){let x=c.getContext('2d');x.clearRect(0,0,700,300);x.font='24px sans-serif';x.fillText(t.value,20,50)}
for(const name of ['keydown','beforeinput','input','keyup'])t.addEventListener(name,e=>{
fetch('/event',{method:'POST',body:JSON.stringify({event:name,trusted:e.isTrusted,value:t.value,key:e.key})});paint()});
c.onclick=e=>{fetch('/event',{method:'POST',body:JSON.stringify({event:'click',trusted:e.isTrusted})});t.focus()};
</script>'''
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        events.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200); self.end_headers()
    def log_message(self,*args): pass


def main():
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}/'
    before=_frontmost()
    cursor=Q.CGEventGetLocation(Q.CGEventCreate(None))
    window=None
    try:
        subprocess.run(['osascript','-e','tell application "Safari" to make new document with properties {URL:'+json.dumps(url)+'}'],check=True,capture_output=True)
        time.sleep(2)
        window=_window_for_url(url)
        target=SafariInput(url)
        computer=None
        if '--bridge' in sys.argv:
            from hunch.safari import SafariComputer
            computer=SafariComputer(allowed_origins=(url,))
            response=computer._call('open',{'url':url,'newWindow':False})
            if response.get('status')!='verified':raise RuntimeError(response)
            computer._bind(response)
        capture=target.screenshot()
        scale=capture['pixelWidth']/target.bounds['Width']
        if computer:
            computer.capture_screenshot()
            result=computer.act([{'action':'click_xy','x':100*scale,'y':180*scale},
                                 {'action':'type','text':'HUNCH INITIAL'},
                                 {'action':'key','key':'a','modifiers':['command']},
                                 {'action':'type','text':'BACKGROUND VERIFIED — ✓'}],detailed=True)
            print('bridge receipt',result['status'],result.get('completedActions'),result.get('reason',''))
            assert result['status']=='performed_unverified' and result['completedActions']==4
        else:
            target.pointer({'action':'click_xy','x':100*scale,'y':180*scale},capture)
            target.type('HUNCH INITIAL')
            target.key('a',['command'])
            target.type('BACKGROUND VERIFIED — ✓')
        time.sleep(.3)
        inputs=[e for e in events if e['event']=='input']
        report={'foregroundUnchanged':_frontmost()==before,
                'cursorUnchanged':Q.CGEventGetLocation(Q.CGEventCreate(None))==cursor,
                'trustedClick':any(e['event']=='click' and e['trusted'] for e in events),
                'trustedInput':bool(inputs) and all(e['trusted'] for e in inputs),
                'value':inputs[-1]['value'] if inputs else None}
        print(json.dumps(report,ensure_ascii=False))
        assert all(report[k] for k in ('foregroundUnchanged','cursorUnchanged','trustedClick','trustedInput'))
        assert report['value']=='BACKGROUND VERIFIED — ✓'
    finally:
        if window is not None:
            subprocess.run(['osascript','-e',f'tell application "Safari" to close window id {window}'],check=True,capture_output=True)
        server.shutdown()

if __name__=='__main__':main()
