#!/usr/bin/env python3
"""Check packaged power descriptors against the canonical contract."""
from pathlib import Path
root = Path(__file__).resolve().parents[1]
source = (root / 'contracts/power-providers.json').read_bytes()
for path in ['blue/src/colors_compute/power-providers.json', 'red/resources/power-providers.json', 'green/src/resources/colors_compute/power-providers.json']:
    assert (root / path).read_bytes() == source, path
print('Power descriptor copies passed')
