#!/usr/bin/env python3
"""Package the provider scripts consumed by managed Kubernetes applications."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
manifest = json.loads((ROOT / 'contracts/managed-artifacts.json').read_text())
content = json.dumps({provider: {name: (ROOT / source).read_text() for name, source in files.items()}
                      for provider, files in manifest.items()}, indent=2, sort_keys=True) + '\n'
for directory in ('blue/src/colors_compute', 'red/resources', 'green/src/resources/colors_compute'):
    target = ROOT / directory / 'managed-artifacts.json'
    if args.write:
        target.write_text(content)
    if not target.exists() or target.read_text() != content:
        raise SystemExit('managed artifact copy differs: ' + str(target.relative_to(ROOT)))
print('Managed application artifact copies: passed')
