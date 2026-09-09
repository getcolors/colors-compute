#!/usr/bin/env python3
"""JSONL driver for the language-independent contract suite."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blue/src"))
from colors_compute import backend_plan, collect, credential_requirements, expand, render_template, state_decision, state_keys, validate

operations = {f.__name__: f for f in (
    backend_plan, collect, credential_requirements, expand, render_template, state_decision, state_keys, validate,
)}
for line in sys.stdin:
    try:
        case = json.loads(line)
        result = operations[case["op"]](*case["args"])
    except Exception as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
