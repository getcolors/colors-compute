#!/usr/bin/env python3
"""Credential-free local OpenTofu state/read-only runner probe; never uses remote state."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def state_probe(tofu):
    environment = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'HOME', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NIX_SSL_CERT_FILE')}
    with tempfile.TemporaryDirectory(prefix='colors-compute-local-probe-') as directory:
        root = Path(directory)
        (root / 'backend.tf.json').write_text(json.dumps({'terraform': {'backend': {'local': {}}}}))
        def run(*args):
            return subprocess.run([str(tofu), *args], cwd=root, env=environment,
                                  capture_output=True, text=True, timeout=20)
        assert run('init', '-input=false', '-no-color').returncode == 0
        absent = run('state', 'pull')
        assert absent.returncode == 0 and absent.stdout == '', 'unexpected absent local-state behavior'
        state = {'version': 4, 'serial': 1, 'lineage': '1c4360cb-c4bb-4671-99c4-e8db78246c3c',
                 'terraform_version': '1.12.5', 'resources': [], 'outputs': {'params': {
                     'value': {'provider': 'vultr', 'ip': '192.0.2.1'},
                     'type': ['object', {'provider': 'string', 'ip': 'string'}]}}}
        (root / 'terraform.tfstate').write_text(json.dumps(state))
        present = run('state', 'pull')
        assert present.returncode == 0
        result = json.loads(present.stdout)
        for field in ('version', 'serial', 'lineage', 'outputs', 'resources'):
            assert result[field] == state[field], f'changed field: {field}'
    return {'absent_exit': absent.returncode, 'absent_stdout_bytes': len(absent.stdout),
            'valid_v4_read': True, 'temporary_directory_removed': not root.exists()}


async def blue_probe(root):
    sys.path.insert(0, str(root / 'blue/src'))
    from colors_compute.backend import _run, read_state, ProcessResult
    # Bounded children are deliberately used so a buggy runner cannot leave a
    # persistent process behind. This is a real local subprocess timeout probe.
    command = [sys.executable, '-c',
               'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'
               '"import time; time.sleep(1.0)"]); time.sleep(1.0)']
    started = time.monotonic()
    try:
        await _run(command, tempfile.gettempdir(), {'PATH': os.defpath}, 150)
    except TimeoutError:
        pass
    elapsed = time.monotonic() - started
    secret = 'fixture-quote"value'
    state = {'version': 4, 'serial': 0, 'lineage': 'fixture', 'resources': [],
             'outputs': {'params': {'value': {'fixture': secret}}}}
    async def runner(*args):
        return ProcessResult(0, json.dumps(state))
    result = await read_state({'provider-backend': 'r2', 'r2-bucket': 'fixture',
                               'r2-endpoint': 'https://example.invalid'}, 'fixture/state',
                              {'COLORS_PAR_R2_ACCESS_KEY_ID': 'fixture-access',
                               'COLORS_PAR_R2_SECRET_ACCESS_KEY': secret}, runner)
    return {'timeout_ms': 150, 'observed_ms': round(elapsed * 1000),
            'descendant_timeout_bounded': elapsed < 0.8,
            'escaped_backend_secret_rejected': result == {'status': 'error'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tofu', required=True, type=Path)
    parser.add_argument('--blue-runner', action='store_true', help='also probe current Blue timeout/redaction behavior')
    args = parser.parse_args()
    result = {'local_state': state_probe(args.tofu.resolve())}
    if args.blue_runner:
        result['blue_runner'] = asyncio.run(blue_probe(Path(__file__).resolve().parents[1]))
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.blue_runner and not all(result['blue_runner'][key] for key in
                                   ('descendant_timeout_bounded', 'escaped_backend_secret_rejected')):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
