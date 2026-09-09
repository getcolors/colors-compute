#!/usr/bin/env python3
"""Verify referenced VPC plans without cloud credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blue/src'))
from colors_compute.planning import plan_deployment

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tofu')
    args = parser.parse_args()
    cases = json.loads((ROOT / 'test/fixtures/network-reference.json').read_text())
    for case in cases:
        if 'error' in case['expected']:
            try:
                plan_deployment(*case['args'])
            except ValueError as error:
                assert str(error) == case['expected']['error']
            else:
                raise AssertionError(case['name'])
            continue
        plan = plan_deployment(*case['args'])
        assert plan == case['expected'], case['name']
        assert plan['shared']['params']['vpc_id'] == case['args'][0].get('digitalocean-vpc-uuid', case['args'][2]['network'].get('id'))
        groups = [plan['documents']['shared'], *plan['documents']['nodes'].values()]
        for group in groups:
            for document in group.values():
                assert 'digitalocean_vpc' not in document.get('resource', {})
        shared = groups[0]['shared-referenced.tf.json']
        assert shared['data']['digitalocean_vpc']['network']['id'] == case['args'][0].get('digitalocean-vpc-uuid', case['args'][2]['network'].get('id'))
        assert shared['output']['params']['value']['network_cidr'] == '${data.digitalocean_vpc.network.ip_range}'
        if args.tofu:
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NIX_SSL_CERT_FILE', 'LANG')}
            for group in groups:
                with tempfile.TemporaryDirectory(prefix='compute-network-reference-') as directory:
                    for name, document in group.items():
                        (Path(directory) / name).write_text(json.dumps(document))
                    for command in (['init', '-backend=false', '-input=false', '-no-color'], ['validate', '-no-color']):
                        result = subprocess.run([str(Path(args.tofu).resolve()), '-chdir=' + directory, *command], env=env, capture_output=True, text=True)
                        if result.returncode:
                            raise AssertionError(result.stdout + result.stderr)
        print(case['name'] + ': passed', flush=True)
    print(f'{len(cases)} referenced network cases passed')

if __name__ == '__main__':
    main()
