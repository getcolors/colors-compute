#!/usr/bin/env python3
"""JSONL driver for the supported single-unit and rendering contracts."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blue/src"))
from colors_compute import registration_plan, compute_node, node_plan, provider_request, backend_plan, credential_requirements, provider_plan, render_template, validate
operations = {f.__name__: f for f in (registration_plan, node_plan, provider_request, backend_plan, credential_requirements, provider_plan, render_template, validate)}
def node_plan_valid(opts, request):
    try:
        node_plan(opts, request)
        return True
    except Exception:
        return False
operations["node_plan_valid"] = node_plan_valid
from colors_compute.diagnostics import sanitize_stderr, credential_values
operations["sanitize_error"] = lambda value, opts, environment: sanitize_stderr(value, credential_values(opts, environment)) or None
operations["node_runtime_error"] = lambda opts, request, operation: asyncio.run(compute_node(opts, request, operation, {}))
for line in sys.stdin:
    try:
        case = json.loads(line)
        result = operations[case["op"]](*case["args"])
    except Exception as error:
        result = {"error": str(error)}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
