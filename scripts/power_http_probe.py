#!/usr/bin/env python3
"""Exercise all three native HTTP transports against synthetic local HTTPS only."""
import argparse
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--bun', default='bun')
parser.add_argument('--bb', default='bb')
args = parser.parse_args()
bun, bb = shutil.which(args.bun), shutil.which(args.bb)
assert bun and bb, 'Bun and Babashka must be available'
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def handle_call(self):
        assert self.headers.get('Authorization') == 'Bearer synthetic-token'
        body = b'x' * 2097153 if self.path == '/large' else b'{"status":"ok"}' if self.path == '/ok' else b''
        self.send_response(302 if self.path == '/redirect' else 200 if body else 204)
        if self.path == '/redirect': self.send_header('Location', '/ok')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError): pass
    do_GET = handle_call
    do_POST = handle_call
    def log_message(self, *_): pass
with tempfile.TemporaryDirectory(prefix='power-http-probe-') as directory:
    work = Path(directory)
    cert, key = work / 'cert.pem', work / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                    '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, capture_output=True)
    key.chmod(0o600)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'https://127.0.0.1:' + str(server.server_address[1])
    env = {'PATH': os.environ['PATH'], 'HOME': directory, 'SSL_CERT_FILE': str(cert), 'NODE_EXTRA_CA_CERTS': str(cert)}
    python = '''import sys
sys.path.insert(0,sys.argv[1])
from colors_compute.power import _get_http
for method,path,expected in [('GET','/ok','{"status":"ok"}'),('POST','/action','')]:
 assert _get_http(method,sys.argv[2]+path,{'Authorization':'Bearer synthetic-token'})==expected
for path in ['/large','/redirect']:
 try: _get_http('GET',sys.argv[2]+path,{'Authorization':'Bearer synthetic-token'})
 except Exception: pass
 else: raise AssertionError('unsafe response accepted')
print('blue native HTTPS passed')
'''
    red = "import {nativePowerHttp} from " + json.dumps(str(ROOT / 'red/src/power.ts')) + "; const url=process.argv[2]; for(const [method,path,expected] of [['GET','/ok','{\"status\":\"ok\"}'],['POST','/action','']]){if(await nativePowerHttp(method,url+path,{Authorization:'Bearer synthetic-token'})!==expected)throw Error('response mismatch');} for(const path of ['/large','/redirect']){let refused=false;try{await nativePowerHttp('GET',url+path,{Authorization:'Bearer synthetic-token'});}catch{refused=true;}if(!refused)throw Error('unsafe response accepted');} console.log('red native HTTPS passed');"
    green = '''(require '[io.github.getcolors.compute-power :as power] '[clojure.java.io :as io])
(import '[java.security KeyStore] '[java.security.cert CertificateFactory] '[javax.net.ssl SSLContext TrustManagerFactory HttpsURLConnection])
(let [[cert url] *command-line-args* store (KeyStore/getInstance (KeyStore/getDefaultType))]
 (.load store nil nil)
 (with-open [stream (io/input-stream cert)] (.setCertificateEntry store "probe" (.generateCertificate (CertificateFactory/getInstance "X.509") stream)))
 (let [factory (TrustManagerFactory/getInstance (TrustManagerFactory/getDefaultAlgorithm)) context (SSLContext/getInstance "TLS")]
  (.init factory store) (.init context nil (.getTrustManagers factory) nil) (HttpsURLConnection/setDefaultSSLSocketFactory (.getSocketFactory context)))
 (doseq [[method path expected] [["GET" "/ok" "{\\"status\\":\\"ok\\"}"] ["POST" "/action" ""]]]
  (assert (= expected (#'power/native-http method (str url path) {"Authorization" "Bearer synthetic-token"})))))
(let [[_ url] *command-line-args*]
 (doseq [path ["/large" "/redirect"]]
  (assert (try (#'power/native-http "GET" (str url path) {"Authorization" "Bearer synthetic-token"}) false (catch Exception _ true)))))
(println "green native HTTPS passed")
'''
    (work/'probe.py').write_text(python)
    (work/'probe.ts').write_text(red)
    (work/'probe.clj').write_text(green)
    try:
        commands = [[shutil.which('python3'),str(work/'probe.py'),str(ROOT/'blue/src'),url],
                    [bun,str(work/'probe.ts'),url],
                    [bb,'-cp',str(ROOT/'green/src/clj')+':'+str(ROOT/'green/src/resources'),str(work/'probe.clj'),str(cert),url]]
        for command in commands:
            result = subprocess.run(command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())
    finally:
        server.shutdown()
        server.server_close()
