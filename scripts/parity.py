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
            raise AssertionError(f'{color}: {case["name"]}\nexpected: {case["expected"]}\nactual: {actual}')


def main():
    cases = json.loads((ROOT / "test/fixtures/contracts.json").read_text())
    cases.extend(json.loads((ROOT / "test/fixtures/coordination.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/managed-journal.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/journal.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/provider-requests.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/lifecycle.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/deployment-requests.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/planning.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/provider-icmp.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/provider-endpoint.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/provider-network-created.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/provider-roles.json").read_text()))
    cases.extend(json.loads((ROOT / "test/fixtures/controllers.json").read_text()))
    for example in json.loads((ROOT / "test/fixtures/provider-plans.json").read_text()):
        directory = ROOT / "providers" / example["provider"] / "examples"
        cases.append({"name": f'packaged {example["provider"]} {example["stage"]}',
                      "op": "provider_plan",
                      "args": [example["provider"], example["stage"], json.loads((directory / "inputs.json").read_text())],
                      "expected": {name: json.loads((directory / source).read_text())
                                   for name, source in example["files"].items()}})
    cases.extend(json.loads((ROOT / "test/fixtures/managed-plans.json").read_text()))
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
