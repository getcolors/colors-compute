#!/usr/bin/env python3
"""Test native absence-verifier wrappers at their subprocess/credential boundary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = {
    'profile': 'test-production', 'provider-compute': 'google',
    'provider-backend': 'r2', 's3-prefix': 'infrastructure',
    'r2-bucket': 'test-state', 'r2-endpoint': 'https://fixture.r2.cloudflarestorage.com',
    'google-project': 'test-project', 'google-zone': 'us-central1-a',
}
SECRETS = {
    'COLORS_PAR_APP_SECRET': 'never-forward-app-secret',
    'COLORS_PAR_RYBBIT_SSH_PASSPHRASE': 'never-forward-passphrase',
    'COLORS_PAR_NEW_PASSPHRASE': 'never-forward-new-passphrase',
    'SSH_AUTH_SOCK': '/never/forward/operator-agent',
    'UNRELATED_TOKEN': 'never-forward-unrelated-token',
}
CREDENTIALS = {
    key: 'fixture-' + key.lower() for key in (
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
        'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_CONFIG_FILE',
        'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CA_BUNDLE',
        'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN', 'AWS_ROLE_SESSION_NAME',
        'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI',
        'AWS_CONTAINER_AUTHORIZATION_TOKEN', 'AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE',
        'AZURE_CONFIG_DIR',
        'GOOGLE_APPLICATION_CREDENTIALS', 'CLOUDSDK_CONFIG',
        'COLORS_PAR_R2_ACCESS_KEY_ID', 'COLORS_PAR_R2_SECRET_ACCESS_KEY',
        'COLORS_PAR_OCI_ACCESS_KEY_ID', 'COLORS_PAR_OCI_SECRET_ACCESS_KEY',
        'COLORS_PAR_DO_TOKEN', 'COLORS_PAR_HCLOUD_TOKEN',
        'COLORS_PAR_VULTR_API_KEY', 'COLORS_PAR_YANDEX_TOKEN',
        'OCI_CLI_CONFIG_FILE', 'OCI_CLI_PROFILE',
    )
}


def programs():
    return {
        'green': [shutil.which('bb'), '-cp', str(ROOT / 'green/src/clj') + ':' + str(ROOT / 'green/src/resources'), '-e', """(require '[cheshire.core :as json] '[io.github.getcolors.compute-ssh :as ssh]) (let [c (json/parse-string (slurp *in*) true) env (into {} (map (fn [[k v]] [(name k) v]) (:environment c)))] (println (json/generate-string (ssh/ssh-verify-absent! (:opts c) (:request c) env))))"""],
        'red': [shutil.which(os.environ.get('BUN', 'bun')), '-e', """import {readFileSync} from 'node:fs';import {ssh_verify_absent} from './red/src/ssh.ts';const c=JSON.parse(readFileSync(0,'utf8'));console.log(JSON.stringify(await ssh_verify_absent(c.opts,c.request,c.environment)));"""],
        'blue': [sys.executable, '-c', """import asyncio,json,sys;from colors_compute.ssh import ssh_verify_absent
c=json.load(sys.stdin)
print(json.dumps(asyncio.run(ssh_verify_absent(c['opts'],c['request'],c['environment']))))"""],
    }


def run(command, payload):
    process = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                             text=True, cwd=ROOT,
                             env=dict(os.environ, PYTHONPATH=str(ROOT / 'blue/src')),
                             timeout=30, check=True)
    return json.loads(process.stdout)


def main():
    commands = programs()
    assert all(command[0] for command in commands.values()), 'bb, bun and python are required'
    count = 0
    with tempfile.TemporaryDirectory(prefix='colors-absence-parity-') as temporary:
        root = Path(temporary)
        capture = root / 'capture.json'
        stub = root / 'python3'
        environment = {**CREDENTIALS, **SECRETS, 'PATH': str(root), 'HOME': str(root), 'TMPDIR': str(root)}
        request = {'workdir': str(root / 'workdir'),
                   'consumers': [{'node_id': 'app-node', 'state_filename': 'app-node.tfstate'}],
                   'registrations': [{'name': 'machine-access', 'state_filename': 'app-registration.tfstate'}]}
        payload = {'opts': {**OPTIONS, 'app-secret': 'never-forward-app-option',
                            'rybbit-ssh-passphrase': 'never-forward-passphrase-option',
                            'google-token': 'never-forward-inline-provider-token',
                            'green/event': 'create'},
                   'request': request, 'environment': environment}
        responses = [
            {'status': 'verified', 'verified_absent': True},
            {'status': 'error', 'error': {'code': 'ssh_consumers_present',
                                         'message': 'existing consumer'}},
            {'status': 'error', 'error': {'code': 'ssh_absence_unverified',
                                         'message': 'provider query failed'}},
        ]
        for response in responses:
            stub.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
message=json.loads(sys.stdin.readline())
Path({str(capture)!r}).write_text(json.dumps({{"message":message,"environment":dict(os.environ)}}))
print({json.dumps(response)!r},flush=True)
''')
            stub.chmod(0o700)
            for color, command in commands.items():
                capture.unlink(missing_ok=True)
                result = run(command, payload)
                assert result == response, (color, result, response)
                observed = json.loads(capture.read_text())
                assert observed['message'] == {'operation': 'verify_absent', 'opts': OPTIONS,
                                               'request': request}, (color, observed['message'])
                child_env = observed['environment']
                assert child_env.get('COLORS_ABSENCE_AUTH_UNSUPPORTED') == '1', color
                for name, value in CREDENTIALS.items():
                    assert child_env.get(name) == value, (color, name)
                assert all(name not in child_env for name in SECRETS), (color, child_env)
                encoded = json.dumps(observed)
                assert 'never-forward-' not in encoded, (color, observed)
                count += 1
        auth_cases = [
            ('google', 'GOOGLE_APPLICATION_CREDENTIALS'),
            ('google', 'GOOGLE_OAUTH_ACCESS_TOKEN'),
            ('google', 'GOOGLE_IMPERSONATE_SERVICE_ACCOUNT'),
            ('google', 'CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT'),
            ('azure', 'ARM_CLIENT_ID'),
            ('oci', 'OCI_REGION'),
            ('aws', 'AWS_ENDPOINT_URL'),
        ]
        for provider, name in auth_cases:
            for color, command in commands.items():
                run(command, {'opts': {**OPTIONS, 'provider-compute': provider}, 'request': request,
                              'environment': {'PATH': str(root), 'HOME': str(root), name: 'override'}})
                observed = json.loads(capture.read_text())
                assert observed['environment'].get('COLORS_ABSENCE_AUTH_UNSUPPORTED') == '1', (color, name)
                count += 1
        # Supported account selection must not spuriously trigger the guard.
        for provider, name in [('google', 'CLOUDSDK_CONFIG'), ('azure', 'AZURE_CONFIG_DIR'),
                               ('aws', 'AWS_PROFILE'), ('aws', 'AWS_ROLE_ARN'),
                               ('aws', 'AWS_CONTAINER_CREDENTIALS_FULL_URI')]:
            for color, command in commands.items():
                run(command, {'opts': {**OPTIONS, 'provider-compute': provider}, 'request': request,
                              'environment': {'PATH': str(root), 'HOME': str(root), name: 'fixture'}})
                observed = json.loads(capture.read_text())
                assert 'COLORS_ABSENCE_AUTH_UNSUPPORTED' not in observed['environment'], (color, name)
                assert observed['environment'][name] == 'fixture', (color, name)
                count += 1
        # Run the packaged adapter itself with no credentials or provider tools.
        # Invalid descriptors must fail before state reads or provider access.
        stub.unlink()
        stub.symlink_to(sys.executable)
        real_request = {**request, 'registrations': []}
        for invalid in [
            {**real_request, 'consumers': []},
            {**real_request, 'workdir': '../escape'},
            {**real_request, 'consumers': [{'node_id': 'app-node', 'state_filename': '../escape.tfstate'}]},
        ]:
            results = {}
            for color, command in commands.items():
                results[color] = run(command, {'opts': OPTIONS, 'request': invalid,
                                               'environment': {'PATH': str(root), 'HOME': str(root)}})
                assert results[color]['status'] == 'error', (color, results[color])
                assert results[color]['error']['message'], (color, results[color])
                count += 1
            assert results['green'] == results['red'] == results['blue'], results
            assert not Path(request['workdir']).exists(), 'invalid request mutated storage'
        for provider, name in auth_cases:
            results = {}
            for color, command in commands.items():
                results[color] = run(command, {
                    'opts': {**OPTIONS, 'provider-compute': provider},
                    'request': {**real_request, 'registrations': request['registrations'] if provider == 'aws' else []},
                    'environment': {'PATH': str(root), 'HOME': str(root), name: 'override'},
                })
                assert results[color]['status'] == 'error', (color, name, results[color])
                assert 'unsupported provider authentication override' in results[color]['error']['message'], (color, name, results[color])
                count += 1
            assert results['green'] == results['red'] == results['blue'], results
            assert not Path(request['workdir']).exists(), 'unsupported auth mutated storage'
    print(f'Native SSH absence wrapper parity: {count} process-boundary cases passed')


if __name__ == '__main__':
    main()
