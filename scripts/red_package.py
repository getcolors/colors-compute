#!/usr/bin/env python3
"""Verify both distribution layouts expose the single-unit API without an SDK copy."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
BUN = os.environ.get('BUN', 'bun')


def run(*args, cwd):
    result = subprocess.run([BUN, *map(str, args)], cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main():
    with tempfile.TemporaryDirectory(prefix='compute-package-') as temporary:
        base = Path(temporary)
        for name, package in [('root', ROOT), ('red', ROOT / 'red')]:
            manifest = json.loads((package / 'package.json').read_text())
            assert 'red' not in manifest.get('dependencies', {})
            assert 'red' not in manifest.get('peerDependencies', {})
            archive = base / (name + '.tgz')
            run('pm', 'pack', '--ignore-scripts', '--filename', archive, cwd=package)
            consumer = base / name
            consumer.mkdir()
            (consumer / 'package.json').write_text(json.dumps({'name': 'compute-consumer', 'private': True,
                'type': 'module', 'dependencies': {'colors-compute-red': str(archive)}}))
            run('install', '--ignore-scripts', cwd=consumer)
            (consumer / 'check.ts').write_text('''
import {strict as assert} from 'node:assert';
import * as compute from 'colors-compute-red';
for (const name of ['node_plan','build_node','compute_node']) assert.equal(typeof compute[name], 'function');
for (const name of ['orchestrate','clusterWorkflow','coordination','journalGet','journalPut','Coordinator']) assert.equal(name in compute, false);
''')
            run('check.ts', cwd=consumer)
            assert not (consumer / 'node_modules/red').exists()
            print(name + ': standalone single-unit API passed')


if __name__ == '__main__':
    main()
