#!/usr/bin/env python3
"""Verify Yandex address selection and optionally validate dynamic provider schemas."""
import argparse,json,os,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'blue/src'))
from colors_compute.provider_request import provider_request
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--tofu');args=parser.parse_args()
cases=json.loads((ROOT/'test/fixtures/yandex-static-ip.json').read_text())
for case in cases:
    try: actual=provider_request(*case['args'])
    except ValueError as error: actual={'error':str(error)}
    assert actual==case['expected'],case['name']
inputs=cases[1]['args'];inputs[0].pop('yandex-image-id',None);inputs[0]['yandex-image-family']='ubuntu-2404-lts';discovery=provider_request(*inputs);assert discovery['stage']=='node-discovery-dynamic'
if args.tofu:
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','SSL_CERT_FILE','SSL_CERT_DIR','NIX_SSL_CERT_FILE','LANG')}
    for docs in [cases[1]['expected']['documents'],discovery['documents']]:
        with tempfile.TemporaryDirectory(prefix='yandex-dynamic-') as directory:
            for name,doc in docs.items():(Path(directory)/name).write_text(json.dumps(doc))
            for command in (['init','-backend=false','-input=false','-no-color'],['validate','-no-color']):
                result=subprocess.run([str(Path(args.tofu).resolve()),'-chdir='+directory,*command],env=env,capture_output=True,text=True)
                if result.returncode:raise AssertionError(result.stdout+result.stderr)
print('Yandex static/dynamic and image-discovery selection passed')
