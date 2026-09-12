from copy import deepcopy
import json
from pathlib import Path
import pytest
from colors_compute.inspection import read_deployment

CASES = json.loads((Path(__file__).parents[2] / 'test/fixtures/lifecycle.json').read_text())
DOCUMENT = next(case['expected']['document'] for case in CASES if case['name'] == 'lifecycle scale-down retains retired node intent')
OPTS = {'profile': 'demo', 'provider-compute': 'vultr', 'provider-backend': 's3',
        's3-bucket': 'states', 's3-region': 'eu-west-1'}


@pytest.mark.asyncio
async def test_inspect_recorded_nodes_without_mutation():
    document = deepcopy(DOCUMENT)
    document['lock'] = {'state': 'idle', 'run_id': None}
    async def read(opts, key, env, include_outputs=False):
        if include_outputs:
            return {'status': 'present', 'params': {'provider': 'vultr'}, 'outputs': {'params': {'provider': 'vultr', 'network_cidr': '10.0.0.0/24'}}}
        node_id = key.rsplit('/', 1)[1].removesuffix('.tfstate')
        return {'status': 'present', 'params': {'node_id': node_id, 'provider': 'vultr', 'name': 'demo-' + node_id, 'ip': '192.0.2.10', 'user': 'root', 'sudoer': 'root'}}
    result = await read_deployment(OPTS, {'HOME': '/home/test'}, {'journal_get': lambda *_: {'status': 'present', 'document': document}, 'read_state': read})
    assert result['status'] == 'present'
    assert len(result['cluster']['nodes']) == len(document['nodes'])
    assert result['key']['private_key_path'] == '/home/test/.ssh/demo'
    assert document['lock']['state'] == 'idle'
    deps = {'journal_get': lambda *_: {'status': 'present', 'document': document}, 'read_state': read}
    entry = result['cluster']['nodes'][-1]['node_id']
    selected = await read_deployment(OPTS, {}, deps, {'entry_node_id': entry})
    assert selected['cluster']['entry_node_id'] == entry
    assert await read_deployment(OPTS, {}, deps, {'entry_node_id': 'missing'}) == {'status': 'error'}


@pytest.mark.asyncio
async def test_inspect_refuses_active_or_unknown_ownership():
    for observed in ({'status': 'error'}, {'status': 'present', 'document': DOCUMENT}):
        assert await read_deployment(OPTS, {}, {'journal_get': lambda *_: observed}) == {'status': 'error'}
    assert await read_deployment(OPTS, {}, {'journal_get': lambda *_: {'status': 'absent'}}) == {'status': 'absent'}


@pytest.mark.asyncio
async def test_inspect_treats_missing_managed_bucket_as_absent():
    def presence(status):
        return {'journal_get': lambda *_: {'status': 'error'}, 'backend_presence': lambda *_: {'status': status}}
    assert await read_deployment(OPTS, {}, presence('absent')) == {'status': 'absent'}
    assert await read_deployment(OPTS, {}, presence('present')) == {'status': 'error'}
    assert await read_deployment(OPTS, {}, presence('skipped')) == {'status': 'error'}
    def refuse(*_):
        raise ValueError('no credentials')
    assert await read_deployment(OPTS, {}, {'journal_get': lambda *_: {'status': 'error'}, 'backend_presence': refuse}) == {'status': 'error'}
    asked = []
    deps = {'journal_get': lambda *_: {'status': 'absent'}, 'backend_presence': lambda *_: asked.append(1) or {'status': 'present'}}
    assert await read_deployment(OPTS, {}, deps) == {'status': 'absent'}
    assert asked == []
