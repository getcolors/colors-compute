#!/usr/bin/env python3
"""JSONL driver for the language-independent contract suite."""
import json
import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blue/src"))
from colors_compute import deployment_requests, plan_deployment, provider_request, coordination, backend_plan, collect, credential_requirements, expand, provider_plan, render_template, state_decision, state_keys, validate

from colors_compute.managed import plan_managed_kubernetes
from colors_compute.controller import controller_artifact
from colors_compute.backend import read_state, ProcessResult
from colors_compute.journal import journal_get, journal_put

def read_state_case(opts, key, environment, responses):
    pending = iter(responses)
    async def runner(*_):
        result = next(pending)
        return ProcessResult(result["exit"], result.get("out", ""), result.get("err", ""))
    return asyncio.run(read_state(opts, key, environment, runner))

def journal_case(opts, environment, response, body, intent=None):
    async def runner(command, *_):
        if command[2] == "get-object":
            Path(command[7]).write_bytes(base64.b64decode(body["bytes_base64"]) if isinstance(body, dict) else body.encode("utf-8"))
        return ProcessResult(response["exit"], response.get("out", ""), response.get("err", ""))
    return asyncio.run(journal_get(opts, environment, runner) if intent is None
                       else journal_put(opts, intent, environment, runner))

operations = {f.__name__: f for f in (
    plan_managed_kubernetes, controller_artifact, deployment_requests, plan_deployment, provider_request, journal_case, coordination, read_state_case, backend_plan, collect, credential_requirements, expand, provider_plan, render_template, state_decision, state_keys, validate,
)}
for line in sys.stdin:
    try:
        case = json.loads(line)
        result = operations[case["op"]](*case["args"])
    except Exception as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
