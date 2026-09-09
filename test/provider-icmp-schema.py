"""Validate ICMP provider documents without backend initialization or cloud access.

Run: python test/provider-icmp-schema.py /absolute/path/to/tofu
Uses the versions pinned by each packaged provider template. Registry downloads
may occur; no plan, apply, credential reads, or remote state operations run.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CASES = json.loads((Path(__file__).parent / 'fixtures/provider-icmp.json').read_text())
TOFU = str(Path(sys.argv[1]).resolve())
ENV = {key: value for key, value in os.environ.items()
       if key in ('HOME', 'PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NIX_SSL_CERT_FILE', 'LANG')}

def check(case):
    with tempfile.TemporaryDirectory(prefix='compute-icmp-schema-') as directory:
        for filename, document in case['expected']['documents'].items():
            Path(directory, filename).write_text(json.dumps(document, indent=2) + '\n')
        for command in [('init', '-backend=false', '-input=false', '-no-color'), ('validate', '-no-color')]:
            result = subprocess.run([TOFU, *command], cwd=directory, env=ENV,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=240)
            if result.returncode:
                raise RuntimeError(case['name'] + ': ' + result.stdout)
    return case['name'] + ': passed'

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    for message in executor.map(check, (case for case in CASES if 'documents' in case['expected'])):
        print(message, flush=True)
