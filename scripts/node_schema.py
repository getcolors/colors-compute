#!/usr/bin/env python3
"""Validate complete single-unit OpenTofu roots without credentials or cloud calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blue/src'))
from colors_compute.node import node_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tofu', required=True)
    parser.add_argument('--provider')
    args = parser.parse_args()
    cases = json.loads((ROOT / 'test/fixtures/node-inputs.json').read_text())
    for case in cases:
        opts, request = case['args']
        provider = opts['provider-compute']
        if args.provider and args.provider != provider:
            continue
        plan = node_plan(opts, request)
        root = plan['documents']['compute.tf.json']
        # SSH authority is owned by the named encrypted SSH resource. Compute
        # roots consume only its public identity and must never generate/export
        # a private key or publish legacy key objects through OpenTofu.
        assert plan['key_objects'] == {}
        assert 'tls_private_key' not in root.get('resource', {})
        assert 'aws_s3_object' not in root.get('resource', {})
        assert 'ssh_private_key' not in root.get('output', {})
        assert 'keys_access_key' not in root.get('variable', {})
        identity = root['output']['compute_identity']['value']
        assert identity['profile'] == opts['profile']
        assert identity['node_id'] == request['node_id']
        assert identity['state_filename'] == request['state_filename']
        assert identity['provider'] == provider
        assert identity['ssh_resource_reference'] == request['ssh_resource']['reference']
        assert identity['ssh_fingerprint'] == request['ssh_resource']['fingerprint']
        serialized = json.dumps(root)
        assert 'private_key' not in serialized
        assert 'colors-shared-reference-' not in serialized and 'colors-public-key-placeholder' not in serialized
        with tempfile.TemporaryDirectory(prefix='compute-schema-') as temporary:
            directory = Path(temporary)
            for name, document in plan['documents'].items():
                (directory / name).write_text(json.dumps(document))
            env = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'LANG', 'TF_PLUGIN_CACHE_DIR')}
            env.update(HOME=temporary, AWS_EC2_METADATA_DISABLED='true', TF_IN_AUTOMATION='1')
            for command in ([args.tofu, 'init', '-backend=false', '-input=false', '-no-color'],
                            [args.tofu, 'validate', '-no-color']):
                result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode:
                    raise RuntimeError(f'{provider}: {result.stdout}\n{result.stderr}')
        print(f'{provider}: public-only node identity and complete schema passed', flush=True)


if __name__ == '__main__':
    main()
