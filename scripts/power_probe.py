#!/usr/bin/env python3
"""Offline OCI CLI argument validation. JSON input generation never calls OCI APIs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--oci', default='oci')
args = parser.parse_args()
executable = shutil.which(args.oci)
if not executable:
    parser.error('OCI CLI unavailable; run under uv --with oci-cli==3.92.1')
with tempfile.TemporaryDirectory(prefix='power-cli-probe-') as root:
    config = Path(root) / 'config'
    config.touch(mode=0o600)
    environment = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
    environment.update(HOME=root, PAGER='cat', OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING='True')
    prefix = [executable, '--config-file', str(config), '--profile', 'SYNTHETIC', '--cli-rc-file', '/dev/null', '--no-retry', '--output', 'json']
    for command in [
        ['compute', 'instance', 'action', '--action', 'SOFTSTOP', '--wait-for-state', 'STOPPED', '--max-wait-seconds', '1'],
        ['compute', 'instance', 'action', '--action', 'START', '--wait-for-state', 'RUNNING', '--max-wait-seconds', '1'],
        ['compute', 'instance', 'get'],
        ['compute', 'instance', 'list-vnics'],
    ]:
        result = subprocess.run(prefix + command + ['--instance-id', 'ocid1.instance.oc1.synthetic', '--generate-full-command-json-input'],
                                env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert isinstance(json.loads(result.stdout), dict)
print('OCI CLI offline power argument probe: four fixed commands accepted; no API operations')
