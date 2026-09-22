#!/usr/bin/env python3
"""Exercise native SSH plans/validation without credentials or storage mutation."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE = {'profile': 'production', 'provider-backend': 'local', 's3-prefix': 'infrastructure'}
REQUEST = {'name': 'app-access', 'workdir': '/tmp/colors-sdk', 'passphrase_env': 'COLORS_PAR_APP_ACCESS_PASSPHRASE'}
BACKENDS = {
    'local': {},
    's3': {'s3-bucket': 'state-bucket', 's3-region': 'eu-west-1'},
    'r2': {'r2-bucket': 'alice-state', 'r2-endpoint': 'https://fixture.r2.cloudflarestorage.com/'},
    'oci': {'oci-bucket': 'state-bucket', 'oci-region': 'eu-frankfurt-1', 'oci-namespace': 'fixture'},
    'gcs': {'gcs-bucket': 'state-bucket'},
}


def main():
    cases = []
    for backend, settings in BACKENDS.items():
        opts = {**BASE, 'provider-backend': backend, **settings}
        cases.append({'name': backend + ' inherited', 'opts': opts, 'request': REQUEST})
        cases.append({'name': backend + ' override', 'opts': BASE, 'request': {**REQUEST, 'backend': {'provider-backend': backend, **settings}}})
        cases.append({'name': backend + ' other workstation', 'opts': opts, 'request': {**REQUEST, 'workdir': '/tmp/other-sdk'}})
    for field, value in [('name', '../escape'), ('workdir', '/tmp/../escape'), ('workdir', 'relative'), ('passphrase_env', 'HOME'), ('backend', {'profile': 'other'}), ('unknown', True)]:
        cases.append({'name': 'invalid request ' + field, 'opts': BASE, 'request': {**REQUEST, field: value}, 'invalid': True})
    for endpoint in ['http://example.com', 'https://user:pass@example.com', 'https://example.com/path', 'https://:bad', 'https://[broken]', 'https://example.com:0', 'https://example.com:99999', 'https://example..com', 'https://bad-.example.com', 'https://example.com.']:
        cases.append({'name': 'invalid endpoint ' + endpoint, 'opts': {**BASE, 'provider-backend': 'r2', 'r2-bucket': 'alice-state', 'r2-endpoint': endpoint}, 'request': REQUEST, 'invalid': True})
    for namespace in ['', None, 'bad.namespace', '${oops}']:
        cases.append({'name': 'invalid OCI namespace ' + str(namespace), 'opts': {**BASE, 'provider-backend': 'oci', 'oci-bucket': 'state-bucket', 'oci-region': 'eu-frankfurt-1', 'oci-namespace': namespace}, 'request': REQUEST, 'invalid': True})
    payload = json.dumps(cases)
    programs = {
        'green': ['bb', '-cp', str(ROOT / 'green/src/clj') + ':' + str(ROOT / 'green/src/resources'), '-e', '''(require '[cheshire.core :as json] '[io.github.getcolors.compute-ssh :as ssh]) (println (json/generate-string (mapv (fn [c] (try (ssh/ssh-plan (:opts c) (:request c)) (catch Exception _ {:error true}))) (json/parse-string (slurp *in*) true))))'''],
        'red': [os.environ.get('BUN', 'bun'), '-e', '''import {readFileSync} from 'node:fs';import {ssh_plan} from './red/src/ssh.ts';console.log(JSON.stringify(JSON.parse(readFileSync(0,'utf8')).map(c=>{try{return ssh_plan(c.opts,c.request);}catch{return {error:true};}})));'''],
        'blue': [sys.executable, '-c', '''import json,sys;from colors_compute.ssh import ssh_plan
out=[]
for c in json.load(sys.stdin):
 try: out.append(ssh_plan(c['opts'],c['request']))
 except Exception: out.append({'error':True})
print(json.dumps(out))'''],
    }
    results = {}
    for color, command in programs.items():
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'blue/src'))
        process = subprocess.run(command, input=payload, capture_output=True, text=True, cwd=ROOT, env=env, check=True)
        values = json.loads(process.stdout)
        assert len(values) == len(cases)
        results[color] = values
    for index, case in enumerate(cases):
        values = {color: results[color][index] for color in programs}
        if case.get('invalid'):
            assert all(v == {'error': True} for v in values.values()), (case['name'], values)
        else:
            assert values['green'] == values['red'] == values['blue'], (case['name'], values)
            value = values['blue']
            assert value['status'] == 'planned'
            assert '/production/ssh/app-access/' in '/' + value['object_key']
            assert value['directory'].endswith('/production/ssh/app-access')
            reference = json.loads(value['reference'])
            assert reference['profile'] == 'production' and reference['name'] == 'app-access'
            assert reference['storage'] == value['storage']
    for offset, backend in enumerate(BACKENDS):
        inherited, override, elsewhere = results['blue'][offset * 3:offset * 3 + 3]
        assert inherited == override
        assert (inherited['reference'] == elsewhere['reference']) == (backend != 'local')
    print(f'Native SSH plan parity: {len(cases)} cases passed in Green, Red, and Blue')


if __name__ == '__main__':
    main()
