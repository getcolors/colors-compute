#!/usr/bin/env python3
"""Exercise native journal transports against a private loopback HTTPS S3 server."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
KEY = '/probe-bucket/probe/compute/coordination.json'
ACCESS = 'synthetic-journal-access'
SECRET = 'synthetic-journal-secret'


class JournalS3(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def respond(self, code, body=b'', etag=None):
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Type', 'application/xml' if code != 200 else 'application/json')
        if etag is not None:
            self.send_header('ETag', etag)
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, name):
        self.respond(code, f'<Error><Code>{name}</Code><Message>synthetic response</Message></Error>'.encode())

    def observe(self):
        auth = self.headers.get('Authorization', '')
        self.server.observed.append({
            'method': self.command,
            'r2_access_selected': f'Credential={ACCESS}/' in auth,
            'session_token_sent': 'x-amz-security-token' in self.headers,
        })
        return urlsplit(self.path).path == KEY

    def do_GET(self):
        with self.server.guard:
            if not self.observe():
                self.server.unexpected.append(self.command)
                return self.error(404, 'NoSuchBucket')
            if self.server.body is None:
                return self.error(404, 'NoSuchKey')
            self.respond(200, self.server.body, self.server.etag)

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        with self.server.guard:
            if not self.observe():
                self.server.unexpected.append(self.command)
                return self.error(404, 'NoSuchBucket')
            none_match, match = self.headers.get('If-None-Match'), self.headers.get('If-Match')
            if none_match == '*':
                allowed = self.server.body is None
            elif match is not None:
                allowed = match == self.server.etag
            else:
                self.server.unexpected.append('unconditional PUT')
                return self.error(400, 'InvalidRequest')
            if not allowed:
                return self.error(412, 'PreconditionFailed')
            try:
                json.loads(body)
            except Exception:
                return self.error(400, 'InvalidRequest')
            self.server.version += 1
            self.server.body = body
            self.server.etag = f'"synthetic-version-{self.server.version}"'
            self.respond(200, etag=self.server.etag)

    def refuse(self):
        self.server.unexpected.append(self.command)
        self.error(405, 'MethodNotAllowed')

    do_DELETE = refuse
    do_POST = refuse
    do_PATCH = refuse


def invoke(color, operation, opts, environment, executables, intent=None):
    payload = json.dumps({'operation': operation, 'opts': opts, 'environment': environment, 'intent': intent})
    if color == 'blue':
        code = '''import asyncio,json,sys
from colors_compute.journal import journal_get,journal_put
v=json.load(sys.stdin)
f=journal_get(v['opts'],v['environment']) if v['operation']=='get' else journal_put(v['opts'],v['intent'],v['environment'])
print(json.dumps(asyncio.run(f)))'''
        command = [sys.executable, '-c', code]
    elif color == 'red':
        code = '''import {journalGet,journalPut} from './src/journal.ts';
const v=JSON.parse(await Bun.stdin.text());
console.log(JSON.stringify(await (v.operation==='get'?journalGet(v.opts,v.environment):journalPut(v.opts,v.intent,v.environment))));'''
        command = [executables['bun'], '-e', code]
    else:
        code = '''(require '[cheshire.core :as json] '[io.github.getcolors.compute-journal :as journal])
(let [{:keys [operation opts environment intent]} (json/parse-string (slurp *in*) true)
      env (into {} (map (fn [[k v]] [(name k) v]) environment))]
 (println (json/generate-string (if (= operation "get") (journal/journal-get opts env) (journal/journal-put opts intent env)))))'''
        command = [executables['bb'], '--config', str(Path(environment['HOME']) / 'bb.edn'),
                   '-cp', str(ROOT / 'green/src/clj') + os.pathsep + str(ROOT / 'green/src/resources'), '-e', code]
    env = {**environment, 'PYTHONPATH': str(ROOT / 'blue/src')}
    result = subprocess.run(command, cwd=ROOT / color, env=env, input=payload,
                            capture_output=True, text=True, timeout=150)
    if result.returncode:
        raise AssertionError(f'{color} transport process failed with exit {result.returncode}')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('aws', 'bb', 'bun', 'openssl'):
        parser.add_argument('--' + name, default=os.environ.get(name.upper(), name))
    args = parser.parse_args()
    executables = {}
    for name in ('aws', 'bb', 'bun', 'openssl'):
        found = shutil.which(getattr(args, name))
        if not found:
            parser.error(f'{name} executable not found')
        executables[name] = str(Path(found).resolve())
    with tempfile.TemporaryDirectory(prefix='colors-compute-journal-probe-') as directory:
        root = Path(directory)
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run([executables['openssl'], 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=127.0.0.1',
                        '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        (root / 'bb.edn').write_text('{}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), JournalS3)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.guard = threading.Lock()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f'https://127.0.0.1:{server.server_port}'
        env = {'PATH': str(Path(executables['aws']).parent) + os.pathsep + os.defpath, 'HOME': directory,
               'AWS_ACCESS_KEY_ID': 'synthetic-ambient-key', 'AWS_SECRET_ACCESS_KEY': 'synthetic-ambient-secret',
               'AWS_SESSION_TOKEN': 'synthetic-ambient-token', 'AWS_PROFILE': 'invalid-profile',
               'AWS_CA_BUNDLE': str(cert), 'COLORS_PAR_R2_ACCESS_KEY_ID': ACCESS,
               'COLORS_PAR_R2_SECRET_ACCESS_KEY': SECRET}
        opts = {'profile': 'probe', 'provider-compute': 'vultr', 'provider-backend': 'r2',
                'r2-bucket': 'probe-bucket', 'r2-endpoint': endpoint}
        identity = {'profile': 'probe', 'provider': 'vultr',
                    'backend': {'kind': 'r2', 'bucket': 'probe-bucket', 'region': 'auto', 'endpoint': endpoint}}
        sys.path.insert(0, str(ROOT / 'blue/src'))
        from colors_compute.coordination import coordination
        try:
            for color in ('blue', 'green', 'red'):
                server.body, server.etag, server.version = None, None, 0
                server.observed, server.unexpected = [], []
                initial = invoke(color, 'get', opts, env, executables)
                assert initial == {'status': 'absent'}, f'{color}: absent read returned {initial}; requests={server.observed}; unexpected={server.unexpected}'
                plans = [coordination({'status': 'absent'}, identity,
                                     {'type': 'acquire', 'run_id': f'run-{n}', 'write_id': f'write-{n}', 'target_etag': None})
                         for n in (1, 2)]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    attempts = list(pool.map(lambda plan: invoke(color, 'put', opts, env, executables, plan), plans))
                assert sorted(r['status'] for r in attempts) == ['conflict', 'written'], f'{color}: acquisition race failed'
                observed = invoke(color, 'get', opts, env, executables)
                assert observed['status'] == 'present'
                owner = observed['document']['lock']['run_id']
                update = coordination(observed, identity, {'type': 'declare', 'run_id': owner, 'write_id': 'write-declare',
                                      'target_etag': observed['etag'], 'topology': [{'count': 2}]})
                assert invoke(color, 'put', opts, env, executables, update)['status'] == 'written'
                assert invoke(color, 'put', opts, env, executables, update) == {'status': 'conflict'}
                latest = invoke(color, 'get', opts, env, executables)
                assert latest['document'] == update['document'] and latest['etag'] != observed['etag']
                assert len(server.observed) == 7, f'{color}: unexpected retry count'
                assert not server.unexpected
                assert all(r['r2_access_selected'] and not r['session_token_sent'] for r in server.observed)
                print(json.dumps({'color': color, 'confirmed_absence': True, 'single_acquisition_winner': True,
                                  'stale_update_refused': True, 'r2_auth_isolated': True, 'requests': len(server.observed)}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    main()
