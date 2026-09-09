#!/usr/bin/env python3
"""Check or synchronize packaged copies of the canonical provider registry."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = ["green/src/resources/colors_compute/providers.json", "red/resources/providers.json",
          "blue/src/colors_compute/providers.json"]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    source = (ROOT / "contracts/providers.json").read_bytes()
    for relative in COPIES:
        target = ROOT / relative
        if args.write:
            target.write_bytes(source)
        if target.read_bytes() != source:
            raise SystemExit(f"registry drift: {relative}")
    print("Registry copies: passed")
