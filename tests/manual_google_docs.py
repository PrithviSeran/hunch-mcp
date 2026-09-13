"""Explicit scratch-document-only Safari integration test driver. No live test runs in pytest."""
import argparse
import base64
import json
from pathlib import Path
from hunch.safari import SafariComputer
from hunch.local_mac import _frontmost

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--actions',default='[]')
    args=parser.parse_args()
    before=_frontmost()
    computer=SafariComputer()
    response=computer._call('open',{'url':args.url,'newWindow':False})
    if response.get('status')!='verified':raise RuntimeError(response)
    computer._bind(response)
    computer.capture_screenshot()
    actions=json.loads(args.actions)
    if actions:
        result=computer.act(actions,detailed=True)
        print(json.dumps({k:v for k,v in result.items() if k!='tree'}),flush=True)
    image=computer.capture_screenshot()
    Path('/private/tmp/hunch-google-docs-current.png').write_bytes(base64.b64decode(image))
    print('foregroundUnchanged',_frontmost()==before,flush=True)
    print('\n'.join(line for line in computer.snapshot().splitlines()
                    if 'Document status:' in line or 'name="Rename"' in line),flush=True)

if __name__=='__main__':main()
