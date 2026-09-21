#!/usr/bin/env python3
"""Compare the actual three-color drivers to explicit common fixture results."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def check_output(color, output, cases):
    lines = output.splitlines()
    if len(lines) != len(cases):
        raise AssertionError(f"{color}: expected {len(cases)} results, received {len(lines)}")
    for case, line in zip(cases, lines):
        actual = json.loads(line)
        if actual != case["expected"]:
            def differences(a, b, path=""):
                if type(a) is not type(b): return [f"{path}: expected {a!r}, got {b!r}"]
                if isinstance(a, dict):
                    result = [f"{path}: missing {sorted(set(a)-set(b))}, extra {sorted(set(b)-set(a))}"] if set(a) != set(b) else []
                    for k in sorted(set(a) & set(b)): result.extend(differences(a[k], b[k], path + "/" + k))
                    return result
                if isinstance(a, list):
                    if len(a) != len(b): return [f"{path}: length {len(a)} != {len(b)}"]
                    return [d for i, (x, y) in enumerate(zip(a, b)) for d in differences(x, y, path + "/" + str(i))]
                return [] if a == b else [f"{path}: expected {a!r}, got {b!r}"]
            raise AssertionError(f'{color}: {case["name"]}\n' + "\n".join(differences(case["expected"], actual)[:10]))


def main():
    supported = {"validate", "credential_requirements", "render_template", "backend_plan", "provider_plan", "provider_request", "node_plan", "node_plan_valid"}
    cases = []
    for filename in ("contracts", "provider-requests", "provider-icmp", "provider-endpoint",
                     "provider-network-created", "provider-roles", "compute-options", "network-none",
                     "yandex-static-ip", "network-reference", "local-backend", "nodes", "node-validation"):
        path = ROOT / "test/fixtures" / (filename + ".json")
        if path.exists():
            cases.extend(c for c in json.loads(path.read_text()) if c["op"] in supported)
    for example in json.loads((ROOT / "test/fixtures/provider-plans.json").read_text()):
        directory = ROOT / "providers" / example["provider"] / "examples"
        cases.append({"name": f'packaged {example["provider"]} {example["stage"]}',
                      "op": "provider_plan",
                      "args": [example["provider"], example["stage"], json.loads((directory / "inputs.json").read_text())],
                      "expected": {name: json.loads((directory / source).read_text())
                                   for name, source in example["files"].items()}})
    home = os.environ["HOME"].rstrip("/")
    cases.append({"name": "local directory defaults under home", "op": "backend_plan",
                  "args": [{"provider-backend": "local"}, "demo/compute/shared.tfstate"],
                  "expected": {"config": {"terraform": {"backend": {"local": {
                      "path": home + "/.local/state/colors/demo/compute/shared.tfstate"}}}},
                      "credential_bindings": {}, "environment": {}}})
    fixture = "".join(json.dumps({"op": case["op"], "args": case["args"]}) + "\n" for case in cases)
    for color, command in {
        "green": [os.environ.get("BB", "bb"), "scripts/contract-green.clj"],
        "red": [os.environ.get("BUN", "bun"), "scripts/contract-red.ts"],
        "blue": [os.environ.get("PYTHON", sys.executable), "scripts/contract-blue.py"],
    }.items():
        result = subprocess.run(command, input=fixture, text=True, capture_output=True, cwd=ROOT, check=True)
        check_output(color, result.stdout, cases)
        print(f"{color}: {len(cases)} parity cases passed")
    # Prove the comparator refuses a divergent result, without altering source.
    changed = [json.dumps(case["expected"]) for case in cases]
    changed[0] = json.dumps({"intentional": "regression"})
    try:
        check_output("regression probe", "\n".join(changed), cases)
    except AssertionError:
        print("Parity regression probe: rejected as expected")
    else:
        raise AssertionError("parity failed to detect intentional divergence")


if __name__ == "__main__":
    main()
