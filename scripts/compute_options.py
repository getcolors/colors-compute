#!/usr/bin/env python3
"""Check optional VM policy fixtures; --tofu validates the reviewed provider schema."""
import argparse,json,os,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'blue/src'))
from colors_compute.provider_request import provider_request
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--tofu');args=parser.parse_args()
cases=json.loads((ROOT/'test/fixtures/compute-options.json').read_text())
for case in cases:
    try: actual=provider_request(*case['args'])
    except ValueError as error: actual={'error':str(error)}
    assert actual==case['expected'],case['name']
if args.tofu:
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','SSL_CERT_FILE','SSL_CERT_DIR','NIX_SSL_CERT_FILE','LANG')}
    checked=set()
    def resource_fields(case):
        return sum(len(resource) for doc in case['expected'].get('documents', {}).values() for instances in doc.get('resource', {}).values() for resource in instances.values())
    for case in sorted(cases, key=resource_fields, reverse=True):
        provider=case['args'][0]['provider-compute']
        if provider in checked or 'documents' not in case['expected']: continue
        checked.add(provider)
        with tempfile.TemporaryDirectory(prefix='compute-options-') as directory:
            for name,doc in case['expected']['documents'].items():(Path(directory)/name).write_text(json.dumps(doc))
            for command in (['init','-backend=false','-input=false','-no-color'],['validate','-no-color']):
                result=subprocess.run([str(Path(args.tofu).resolve()),'-chdir='+directory,*command],env=env,capture_output=True,text=True)
                if result.returncode:raise AssertionError(result.stdout+result.stderr)
print(str(len(cases))+' optional compute policy fixtures passed')
