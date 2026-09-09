import asyncio

import pytest
from blue.workflow import run

from colors_compute import expand
from colors_compute.workflow import cluster_workflow


def params(request):
    return {"node_id": request["node_id"], "name": "test-" + request["node_id"],
            "provider": "vultr", "ip": "192.0.2.10", "user": "root", "sudoer": "root"}


@pytest.mark.asyncio
async def test_real_sdk_fanout_waits_for_every_node_then_collects_in_topology_order():
    requests = expand([{"role": "broker", "count": 3}])
    completed = []
    all_started = asyncio.Event()
    started = set()

    async def node_step(opts):
        request = opts["colors-compute/request"]
        started.add(request["node_id"])
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=2)
        await asyncio.sleep((2 - request["index"]) * .01)
        completed.append(request["node_id"])
        return {**opts, "colors-compute/params": {**params(request), "metadata": {"index": request["index"]}}}

    def downstream(opts):
        assert len(completed) == 3
        return {**opts, "inventory": [node["node_id"] for node in opts["colors-compute/cluster"]["nodes"]]}

    result = await run(cluster_workflow(requests, "broker-0", node_step, downstream), {})
    assert result["blue/exit"] == 0
    assert completed == ["broker-2", "broker-1", "broker-0"]
    assert result["inventory"] == ["broker-0", "broker-1", "broker-2"]
    assert result["colors-compute/cluster"]["nodes"][2]["metadata"] == {"index": 2}


@pytest.mark.asyncio
async def test_failed_branch_prevents_downstream_and_retains_branch_results():
    requests = expand([{"role": None, "count": 2}])
    called = []

    def node_step(opts):
        request = opts["colors-compute/request"]
        if request["node_id"] == "1":
            return {**opts, "blue/exit": 7, "blue/err": "node 1 failed"}
        return {**opts, "colors-compute/params": params(request)}

    result = await run(cluster_workflow(requests, "0", node_step, lambda opts: called.append(opts)), {})
    assert result["blue/exit"] == 7
    assert not called
    assert "colors-compute/cluster" not in result
    assert {b["colors-compute/request"]["node_id"] for b in result["blue/branches"]} == {"0", "1"}


@pytest.mark.asyncio
async def test_single_node_uses_same_callback_and_validates_result():
    def node_step(opts):
        return {**opts, "colors-compute/params": params(opts["colors-compute/request"])}
    result = await run(cluster_workflow(expand([{"role": None}]), "0", node_step), {})
    assert result["blue/exit"] == 0
    assert len(result["colors-compute/cluster"]["nodes"]) == 1


@pytest.mark.asyncio
async def test_incomplete_success_cannot_reach_ansible():
    called = []
    result = await run(cluster_workflow(expand([{"role": None}]), "0", lambda o: o,
                                       lambda o: called.append(o)), {})
    assert result["blue/exit"] > 0
    assert not called


def test_invalid_topology_rejected_before_node_calls():
    with pytest.raises(ValueError, match="unknown entry node"):
        cluster_workflow(expand([{"role": None}]), "99", lambda o: o)


@pytest.mark.asyncio
async def test_stale_parent_results_cannot_satisfy_a_new_node_operation():
    old = params({"node_id": "0"})
    result = await run(cluster_workflow(expand([{"role": None}]), "0", lambda o: o), {
        "colors-compute/params": old, "colors-compute/cluster": {"nodes": [old]},
        "blue/branches": [{"colors-compute/params": old}],
    })
    assert result["blue/exit"] > 0
    assert result["blue/err"] == "missing node: 0"
    assert "colors-compute/cluster" not in result
