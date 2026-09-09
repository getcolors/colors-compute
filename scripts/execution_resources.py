#!/usr/bin/env python3
"""Synchronize reviewed execution policy into language distributions."""
from pathlib import Path
root = Path(__file__).resolve().parents[1]
source = (root / 'contracts/execution-policy.json').read_bytes()
for path in ['blue/src/colors_compute/execution-policy.json', 'red/resources/execution-policy.json', 'green/src/resources/colors_compute/execution-policy.json']:
    (root/path).write_bytes(source)
