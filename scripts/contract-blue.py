#!/usr/bin/env python3
"""JSONL driver for the language-independent contract suite."""
import json
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blue/src"))
from colors_compute import coordination, backend_plan, collect, credential_requirements, expand, provider_plan, render_template, state_decision, state_keys, validate

from colors_compute.backend import read_state, ProcessResult

def read_state_case(opts, key, environment, responses):
    pending = iter(responses)
    async def runner(*_):
        result = next(pending)
        return ProcessResult(result["exit"], result.get("out", ""), result.get("err", ""))
    return asyncio.run(read_state(opts, key, environment, runner))

operations = {f.__name__: f for f in (
    coordination, read_state_case, backend_plan, collect, credential_requirements, expand, provider_plan, render_template, state_decision, state_keys, validate,
)}
for line in sys.stdin:
    try:
        case = json.loads(line)
        result = operations[case["op"]](*case["args"])
    except Exception as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
