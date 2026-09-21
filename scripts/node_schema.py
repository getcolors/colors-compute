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
        assert root['resource']['tls_private_key']['machine']['algorithm'] == 'ED25519'
        local = opts['provider-backend'] == 'local'
        dependencies = {'tls_private_key.machine'} if local else {'aws_s3_object.ssh_private', 'aws_s3_object.ssh_public'}
        if local:
            assert not plan['key_objects']
            assert 'aws_s3_object' not in root['resource']
            assert root['output']['ssh_private_key']['sensitive'] is True
            assert 'keys_access_key' not in root.get('variable', {})
        else:
            objects = root['resource']['aws_s3_object']
            assert set(objects) == {'ssh_private', 'ssh_public'}
            for obj in objects.values():
                assert obj['provider'] == 'aws.keys'
                assert 'source' not in obj, 'local keys must never be uploaded'
                assert not obj.get('force_destroy', False), 'legal holds must not be bypassed'
        for resource_type, instances in root['resource'].items():
            if resource_type in ('tls_private_key', 'aws_s3_object'):
                continue
            for resource in instances.values():
                assert dependencies <= set(resource['depends_on'])
        assert root['output']['compute_identity']['value']['node_id'] == request['node_id']
        serialized = json.dumps(root)
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
        print(f'{provider}: complete node schema and dependency graph passed', flush=True)


if __name__ == '__main__':
    main()
