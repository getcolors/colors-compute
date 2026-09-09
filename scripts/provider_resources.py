#!/usr/bin/env python3
"""Package canonical provider templates for independent language distributions."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = ["blue/src/colors_compute/templates.json", "red/resources/templates.json",
          "green/src/resources/colors_compute/templates.json"]


def bundle():
    providers = {}
    for directory in sorted((ROOT / "providers").iterdir()):
        if not directory.is_dir():
            continue
        documents = {source.name.removesuffix(".template"): json.loads(source.read_text())
                     for source in sorted(directory.glob("*.tf.json.template"))}
        if not documents:
            continue
        if not {"shared.tf.json", "node.tf.json"} <= documents.keys():
            raise ValueError(f"incomplete provider template bundle: {directory.name}")
        shared = {"shared.tf.json": documents["shared.tf.json"]}
        keygen = dict(shared)
        if "shared-keygen.tf.json" in documents:
            keygen["shared-keygen.tf.json"] = documents["shared-keygen.tf.json"]
        stages = {"node": {"node.tf.json": documents["node.tf.json"]},
                  "shared": shared, "shared-keygen": keygen}
        manifest = directory / "stages.json"
        if manifest.exists():
            stages = {stage: {name: documents[name] for name in names}
                      for stage, names in json.loads(manifest.read_text()).items()}
        covered = {name for stage in stages.values() for name in stage}
        if covered != documents.keys():
            raise ValueError(f"unmapped provider templates: {directory.name}")
        providers[directory.name] = stages
    return providers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    content = json.dumps(bundle(), indent=2, sort_keys=True) + "\n"
    for relative in COPIES:
        path = ROOT / relative
        if args.write:
            path.write_text(content)
        if not path.exists() or path.read_text() != content:
            raise SystemExit(f"provider resource drift: {relative}; run scripts/provider_resources.py --write")
    print(f"Provider resource copies: {len(bundle())} providers passed")


if __name__ == "__main__":
    main()
