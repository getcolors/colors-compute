#!/usr/bin/env python3
"""Install both Red package layouts and verify they use the consumer's SDK."""

import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BUN = os.environ.get("BUN", "bun")


def run(*args, cwd):
    result = subprocess.run(
        [BUN, *map(str, args)], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main():
    with tempfile.TemporaryDirectory(prefix="colors-red-package-") as directory:
        temp = Path(directory)
        # Use the SDK installed by the frozen development lockfile. Keep the
        # package resolver away from shared caches and registry metadata.
        sdk = temp / "sdk.tgz"
        run("pm", "pack", "--ignore-scripts", "--filename", sdk,
            cwd=ROOT / "red/node_modules/red")
        zod = temp / "zod.tgz"
        run("pm", "pack", "--ignore-scripts", "--filename", zod,
            cwd=ROOT / "red/node_modules/zod")
        for name, package in [("root", ROOT), ("red", ROOT / "red")]:
            manifest = json.loads((package / "package.json").read_text())
            assert "red" not in manifest.get("dependencies", {}), name
            assert manifest["peerDependencies"]["red"] == "*", name
            archive = temp / f"{name}.tgz"
            run("pm", "pack", "--ignore-scripts", "--filename", archive, cwd=package)
            consumer = temp / name
            consumer.mkdir()
            (consumer / "package.json").write_text(json.dumps({
                "name": "sdk-consumer", "private": True, "type": "module",
                "dependencies": {
                    "colors-compute-red": str(archive),
                    "red": str(sdk),
                    "zod": str(zod),
                },
                "overrides": {"zod": str(zod)},
            }))
            run("install", "--ignore-scripts", "--cache-dir", temp / "cache", cwd=consumer)
            (consumer / "check.ts").write_text("""
import { strict as assert } from 'node:assert';
import { dirname } from 'node:path';
import { realpathSync } from 'node:fs';
const consumer = Bun.resolveSync('red', process.cwd());
const library = Bun.resolveSync('colors-compute-red', process.cwd());
const librarySdk = Bun.resolveSync('red', dirname(library));
assert.equal(realpathSync(librarySdk), realpathSync(consumer));
assert.equal(await import(librarySdk), await import(consumer));
const compute = await import('colors-compute-red');
assert.equal(typeof compute.orchestrate, 'function');
assert.equal(typeof compute.clusterWorkflow, 'function');
""")
            run("check.ts", cwd=consumer)
            assert not (consumer / "node_modules/colors-compute-red/node_modules/red").exists(), name
            print(f"{name} package uses the consumer's Red SDK without a nested copy")


if __name__ == "__main__":
    main()
