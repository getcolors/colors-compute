"""Colors SDK fan-out over the common node step, followed by a checked join.

This constructor composes supplied node operations. It does not itself prepare
keys, acquire remote deployment ownership, or execute OpenTofu.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from blue.workflow import Workflow, workflow

from .contract import collect, state_keys


def cluster_workflow(requests: list[dict], entry_node_id: str,
                     node_step: Callable, downstream: Callable | None = None) -> Workflow:
    if not requests:
        raise ValueError("no nodes requested")
    seen = set()
    for request in requests:
        node_id = request.get("node_id")
        if node_id in seen:
            raise ValueError(f"duplicate requested node: {node_id}")
        seen.add(node_id)
    state_keys("validation", [request.get("node_id") for request in requests])
    if entry_node_id not in seen:
        raise ValueError(f"unknown entry node: {entry_node_id}")
    if not callable(node_step):
        raise ValueError("node-step must be callable")
    declared = deepcopy(requests)

    def dispatch(opts):
        return {key: value for key, value in opts.items()
                if key not in ("colors-compute/cluster", "colors-compute/params", "blue/branches")}

    def join(opts):
        branches = opts.get("blue/branches") or [opts]
        result = collect(declared, [branch.get("colors-compute/params") or {} for branch in branches], entry_node_id)
        return {**opts, "colors-compute/cluster": result}

    def wire_fn(step, _run_opts):
        return {
            "colors-compute/dispatch": (dispatch, "colors-compute/node"),
            "colors-compute/node": (node_step, "colors-compute/join"),
            "colors-compute/join": ((join, "colors-compute/downstream") if downstream else (join,)),
            "colors-compute/downstream": (downstream,),
        }.get(step)

    def next_fn(step, successors, opts):
        if (opts.get("blue/exit") or 0) > 0:
            return []
        if step == "colors-compute/dispatch":
            return [("colors-compute/node", {**opts, "colors-compute/request": deepcopy(request)})
                    for request in declared]
        return [(successor, opts) for successor in successors or []]

    return workflow(start="colors-compute/dispatch", wire_fn=wire_fn, next_fn=next_fn)
