#!/usr/bin/env python3
"""Execute managed provider scripts against command doubles, without cloud access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = json.loads((ROOT / 'red/resources/managed-artifacts.json').read_text())

def run(provider, operation, response, snapshot=None, sources=(), credentials=True):
    with tempfile.TemporaryDirectory(prefix='managed-provider-probe-') as directory:
        root = Path(directory)
        commands = root / 'bin'
        commands.mkdir()
        for name, content in ARTIFACTS[provider].items():
            (root / name).write_text(content)
        curl = commands / 'curl'
        curl.write_text('''#!/usr/bin/env python3
import os,sys
assert 'fixture-provider-token' not in ' '.join(sys.argv)
assert sys.stdin.read().strip() == 'Authorization: Bearer fixture-provider-token'
print(os.environ['RESPONSE'])
''')
        curl.chmod(0o755)
        sleep = commands / 'sleep'
        sleep.write_text('#!/usr/bin/env python3\n')
        sleep.chmod(0o755)
        path = root / 'snapshot.json'
        path.write_text(json.dumps(snapshot or {'volumes': ['owned-volume'], 'lb_ip': '192.0.2.1'}))
        env = {key: value for key, value in os.environ.items() if not key.startswith('COLORS_PAR_')}
        env.update(PATH=str(commands) + ':' + os.environ['PATH'], RESPONSE=json.dumps(response))
        if credentials:
            env['COLORS_PAR_DO_TOKEN' if provider == 'digitalocean' else 'COLORS_PAR_VULTR_API_KEY'] = 'fixture-provider-token'
        args = [str(path)] if operation == 'cleanup' else ['192.0.2.1', *sources]
        result = subprocess.run(['bash', str(root / ('managed-' + operation + '.sh')), *args],
                                env=env, capture_output=True, text=True, timeout=20)
        assert 'fixture-provider-token' not in result.stdout + result.stderr
        return result.returncode

for provider, volumes, address in [('vultr', 'blocks', 'ipv4'), ('digitalocean', 'volumes', 'ip')]:
    empty = {volumes: [], 'load_balancers': []}
    assert run(provider, 'cleanup', empty) == 0
    for response in [
        {volumes: [{'id': 'owned-volume'}], 'load_balancers': []},
        {volumes: [], 'load_balancers': [{address: '192.0.2.1'}]},
        {'error': 'unavailable'},
        {**empty, 'links': {'pages': {'next': 'more'}}},
    ]:
        assert run(provider, 'cleanup', response) != 0, (provider, response)
    assert run(provider, 'cleanup', empty, credentials=False) != 0
    assert run(provider, 'cleanup', empty, snapshot={'volumes': 'invalid', 'lb_ip': ''}) != 0
    print(provider + ': cleanup absence, leftovers, malformed data, pagination, credentials, and snapshot checks passed', flush=True)

for sources, allow, expected in [
    (['0.0.0.0/0'], [], 0),
    (['198.51.100.0/24'], ['cidr:198.51.100.0/24'], 0),
    (['198.51.100.0/24'], ['cidr:0.0.0.0/0'], 1),
]:
    response = {'load_balancers': [{'ip': '192.0.2.1', 'firewall': {'allow': allow}}]}
    assert (run('digitalocean', 'ingress', response, sources=sources) == 0) == (expected == 0)
assert run('digitalocean', 'ingress', {'error': 'unavailable'}, sources=['0.0.0.0/0']) != 0
print('digitalocean: ingress policy and malformed response checks passed')
