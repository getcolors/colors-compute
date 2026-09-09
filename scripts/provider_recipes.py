#!/usr/bin/env python3
"""Keep packaged request mappings byte-identical to the canonical library data."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
for filename in ('provider-recipes.json', 'registration-preflight.json'):
    source = (ROOT / 'contracts' / filename).read_bytes()
    for directory in ('blue/src/colors_compute', 'red/resources', 'green/src/resources/colors_compute'):
        path = ROOT / directory / filename
        if args.write:
            path.write_bytes(source)
        elif not path.exists() or path.read_bytes() != source:
            raise SystemExit(f'packaged recipe differs: {path.relative_to(ROOT)}')
print('Provider recipe copies: passed')
