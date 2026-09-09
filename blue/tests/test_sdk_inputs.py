from copy import deepcopy
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from blue.workflow import workflow, run

from colors_compute._copy import deepcopy as clone
from colors_compute.planning import plan_deployment
from colors_compute.orchestration import orchestrate
from test_orchestration import Runtime
from test_coordinator import OPTS


@pytest.mark.asyncio
async def test_real_sdk_frozen_inputs_can_plan_and_orchestrate():
    fixtures = json.loads((Path(__file__).resolve().parents[2] / 'test/fixtures/provider-requests.json').read_text())
    sample = next(case for case in fixtures if case['args'][0]['provider-compute'] == 'vultr')
    opts = {**sample['args'][0], **OPTS, 'compute-prevent-destroy': False}
    request = sample['args'][2]
    requirements = {field: deepcopy(request[field]) for field in ('network', 'security')}
    runtime = Runtime()
    async def step(values):
        # The SDK supplies its actual nested FrozenDict/FrozenList wrappers.
        planned = plan_deployment(values, values['topology'], values['requirements'])
        assert planned['status'] == 'planned' and len(planned['cluster']['nodes']) == 2
        result = await orchestrate(values, values['topology'], values['requirements'], {}, runtime.dependencies())
        assert result['status'] == 'ready'
        return {**values, 'done': True}
    result = await run(workflow(start='compute', wire_fn=lambda *_: (step,)), {**opts, 'topology': [{'count': 2}], 'requirements': requirements})
    assert result['blue/exit'] == 0, result.get('blue/err')
    assert result['done'] is True


def test_clone_handles_immutable_mapping_and_sequence_without_mutating_input():
    source = MappingProxyType({'nodes': ({'id': '0'},)})
    result = clone(source)
    result['nodes'][0]['id'] = 'changed'
    assert source['nodes'][0]['id'] == '0'
