#!/usr/bin/env python3
"""Package reviewed optional compute capability descriptors."""
import argparse
from pathlib import Path
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
source = (root / 'contracts/compute-options.json').read_bytes()
for relative in ['blue/src/colors_compute/compute-options.json','red/resources/compute-options.json','green/src/resources/colors_compute/compute-options.json']:
    path = root / relative
    if args.write:
        path.write_bytes(source)
    if path.read_bytes() != source:
        raise SystemExit('compute options resource drift: ' + relative)
print('Compute options resource copies passed')
