import json
from copy import deepcopy
from pathlib import Path

import pytest

from colors_compute.coordination import coordination, _document
from colors_compute.coordinator import Coordinator
from test_coordinator import Store, OPTS

FIXTURES = json.loads((Path(__file__).resolve().parents[2] / 'test/fixtures/lifecycle.json').read_text())


@pytest.mark.parametrize('case', FIXTURES, ids=lambda case: case['name'])
def test_lifecycle_contract(case):
    args = deepcopy(case['args'])
    if 'error' in case['expected']:
        with pytest.raises(ValueError) as failure:
            coordination(*args)
        assert str(failure.value) == case['expected']['error']
    else:
        actual = coordination(*args)
        assert actual == case['expected']
        assert _document(actual['document'])
        actual['document']['identity']['profile'] = 'changed'
    assert args == case['args']


@pytest.mark.asyncio
async def test_lifecycle_coordinator_create_destroy_retire_recreate():
    store = Store()
    c = Coordinator(OPTS, {}, store.read, store.write, event_prefix='lifecycle/')
    await c.acquire()
    await c.declare([{'count': 1}])
    await c.transition('key-intent', mode='external')
    await c.transition('key-prepared', fingerprint=None)
    shared = await c.shared_start()
    with pytest.raises(ValueError, match='operations outstanding'):
        await c.release()
    await c.shared_complete(shared)
    node = await c.start('0')
    await c.complete('0', node)
    await c.transition('begin-delete')
    node = await c.destroy('0')
    await c.complete('0', node)
    shared = await c.shared_destroy()
    await c.shared_complete(shared)
    await c.transition('key-cleanup')
    await c.transition('key-removed')
    await c.transition('retire')
    await c.transition('recreate')
    assert (await c.snapshot())['document']['generation'] == 2
    await c.release()


@pytest.mark.asyncio
async def test_lifecycle_shared_failed_cas_poison_and_transition_cannot_start():
    store = Store()
    c = Coordinator(OPTS, {}, store.read, store.write, event_prefix='lifecycle/')
    await c.acquire()
    await c.declare([{'count': 1}])
    await c.transition('key-intent', mode='external')
    await c.transition('key-prepared', fingerprint=None)
    with pytest.raises(ValueError, match='invalid coordination transition'):
        await c.transition('shared-start', operation_id='bypass')
    shared = await c.shared_start()
    store.fault = 'lost'
    await c.shared_fail(shared)
    await c.transition('shared-retry', evidence='readable-state')
    shared = await c.shared_start()
    store.fault = 'conflict'
    with pytest.raises(RuntimeError, match='ownership uncertain'):
        await c.shared_complete(shared)
    with pytest.raises(RuntimeError, match='ownership uncertain'):
        await c.release()
