#!/usr/bin/env python3
"""Verify public-only plans; optionally validate schemas without cloud credentials."""
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
    cases = json.loads((ROOT / 'test/fixtures/network-none.json').read_text())
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
        assert plan['cluster']['nodes'][0]['vpc_ip'] is None
        assert not ({'vpc_id', 'network_cidr'} & plan['shared']['params'].keys())
        groups = [plan['documents']['shared'], *plan['documents']['nodes'].values()]
        serialized = json.dumps(groups)
        for token in ('vultr_vpc', 'hcloud_network', 'digitalocean_vpc', 'vpc_uuid', 'vpc_ids', 'hcloud_server_network'):
            assert token not in serialized, token
        if args.tofu:
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NIX_SSL_CERT_FILE', 'LANG')}
            for group in groups:
                with tempfile.TemporaryDirectory(prefix='compute-network-none-') as directory:
                    for name, document in group.items():
                        (Path(directory) / name).write_text(json.dumps(document))
                    for command in (['init', '-backend=false', '-input=false', '-no-color'], ['validate', '-no-color']):
                        result = subprocess.run([str(Path(args.tofu).resolve()), '-chdir=' + directory, *command], env=env, capture_output=True, text=True)
                        if result.returncode:
                            raise AssertionError(result.stdout + result.stderr)
        print(case['name'] + ': passed', flush=True)
    print(f'{len(cases)} public-only network cases passed')

if __name__ == '__main__':
    main()
