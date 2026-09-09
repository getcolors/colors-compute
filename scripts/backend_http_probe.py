#!/usr/bin/env python3
"""Probe actual OpenTofu R2 reads against a loopback-only synthetic S3 server.

No cloud endpoints, account credentials, or state mutations are used. The server
refuses writes and records only auth-selection booleans, never Authorization.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
KEY = 'probe/compute/shared.tfstate'
ACCESS = 'synthetic-r2-access'
SECRET = 'synthetic-r2-secret-only-for-loopback'
PARAMS = {'provider': 'aws', 'ip': '192.0.2.1'}
STATE = json.dumps({'version': 4, 'serial': 1,
                   'lineage': '1c4360cb-c4bb-4671-99c4-e8db78246c3c',
                   'terraform_version': '1.12.5', 'resources': [],
                   'outputs': {'params': {'value': PARAMS,
                                         'type': ['object', {'provider': 'string', 'ip': 'string'}]}}}).encode()


class SyntheticS3(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, code, payload=b'', content_type='application/octet-stream'):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('ETag', '"synthetic-state-etag"')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def handle_read(self):
        auth = self.headers.get('Authorization', '')
        self.server.observed.append({
            'method': self.command,
            'r2_access_selected': f'Credential={ACCESS}/' in auth,
            'ambient_access_selected': 'Credential=synthetic-ambient-aws/' in auth,
            'session_token_sent': 'x-amz-security-token' in self.headers,
        })
        location = urlsplit(self.path)
        if location.path in ('/probe-bucket', '/probe-bucket/') and location.query:
            self.respond(200, b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>probe-bucket</Name><Prefix></Prefix><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated></ListBucketResult>', 'application/xml')
        elif location.path == f'/probe-bucket/{KEY}':
            self.respond(200, STATE)
        elif location.path in ('/probe-bucket', '/probe-bucket/'):
            self.respond(200)
        else:
            self.server.unexpected.append(self.command + ' ' + location.path)
            self.respond(404, b'<Error><Code>NoSuchKey</Code></Error>', 'application/xml')

    do_HEAD = handle_read
    do_GET = handle_read

    def reject_write(self):
        self.server.unexpected.append(self.command)
        self.respond(405, b'<Error><Code>MethodNotAllowed</Code></Error>', 'application/xml')

    do_PUT = reject_write
    do_DELETE = reject_write
    do_POST = reject_write
    do_PATCH = reject_write


def invoke(color, opts, environment, bb, bun):
    payload = json.dumps({'opts': opts, 'key': KEY, 'environment': environment})
    if color == 'blue':
        sys.path.insert(0, str(ROOT / 'blue/src'))
        from colors_compute.backend import read_state
        return asyncio.run(read_state(opts, KEY, environment))
    if color == 'green':
        code = '''(require '[cheshire.core :as json] '[io.github.getcolors.compute-runtime :as runtime])
(let [{:keys [opts key environment]} (json/parse-string (slurp *in*) true)]
 (println (json/generate-string (runtime/read-state opts key (into {} (map (fn [[k v]] [(name k) v]) environment))))))'''
        command = [str(bb), '--config', str(Path(environment['HOME']) / 'bb.edn'),
                   '-cp', str(ROOT / 'green/src/clj') + os.pathsep + str(ROOT / 'green/src/resources'), '-e', code]
    else:
        code = '''import {readState} from './src/backend.ts';
const {opts,key,environment}=JSON.parse(await Bun.stdin.text());
console.log(JSON.stringify(await readState(opts,key,environment)));'''
        command = [str(bun), '-e', code]
    completed = subprocess.run(command, cwd=ROOT / color, input=payload,
                               capture_output=True, text=True, timeout=30, env=environment)
    if completed.returncode:
        raise AssertionError(f'{color} reader process failed with exit {completed.returncode}')
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tofu', default=os.environ.get('TOFU', 'tofu'))
    parser.add_argument('--bb', default=os.environ.get('BB', 'bb'))
    parser.add_argument('--bun', default=os.environ.get('BUN', 'bun'))
    parser.add_argument('--colors', nargs='+', choices=['blue', 'green', 'red'], default=['blue', 'green', 'red'])
    args = parser.parse_args()
    for name in ('tofu', 'bb', 'bun'):
        if name == 'bb' and 'green' not in args.colors or name == 'bun' and 'red' not in args.colors:
            continue
        executable = shutil.which(str(getattr(args, name)))
        if not executable:
            parser.error(f'{name} executable not found')
        setattr(args, name, Path(executable).resolve())
    server = ThreadingHTTPServer(('127.0.0.1', 0), SyntheticS3)
    server.observed, server.unexpected = [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='colors-compute-http-probe-') as directory:
            (Path(directory) / 'bb.edn').write_text('{}')
            env = {'PATH': str(args.tofu.resolve().parent) + os.pathsep + os.defpath,
                   'HOME': directory, 'AWS_ACCESS_KEY_ID': 'synthetic-ambient-aws',
                   'AWS_SECRET_ACCESS_KEY': 'synthetic-ambient-secret',
                   'AWS_SESSION_TOKEN': 'synthetic-ambient-session',
                   'AWS_SECURITY_TOKEN': 'synthetic-ambient-legacy-session',
                   'AWS_PROFILE': 'missing-synthetic-profile',
                   'AWS_DEFAULT_PROFILE': 'missing-synthetic-default-profile',
                   'AWS_EC2_METADATA_DISABLED': 'true',
                   'COLORS_PAR_R2_ACCESS_KEY_ID': ACCESS,
                   'COLORS_PAR_R2_SECRET_ACCESS_KEY': SECRET}
            opts = {'provider-backend': 'r2', 'r2-bucket': 'probe-bucket',
                    'r2-endpoint': f'http://127.0.0.1:{server.server_port}'}
            for color in args.colors:
                server.observed.clear()
                server.unexpected.clear()
                result = invoke(color, opts, env, args.bb, args.bun)
                summary = {'color': color, 'present': result == {'status': 'present', 'params': PARAMS},
                           'requests': list(server.observed), 'unexpected': list(server.unexpected)}
                print(json.dumps(summary), flush=True)
                assert summary['present'], f'{color}: actual reader did not read synthetic state'
                assert server.observed, f'{color}: no S3 requests observed'
                assert not server.unexpected, f'{color}: unexpected request or attempted mutation'
                assert all(r['r2_access_selected'] and not r['ambient_access_selected'] and not r['session_token_sent']
                           for r in server.observed), f'{color}: R2 backend credential isolation failed'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    main()
