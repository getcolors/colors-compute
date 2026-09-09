import asyncio
from copy import deepcopy

import pytest

from colors_compute.coordinator import Coordinator

OPTS = {'profile': 'demo', 'provider-compute': 'vultr', 'provider-backend': 's3',
        's3-bucket': 'states', 's3-region': 'eu-west-1'}


class Store:
    def __init__(self):
        self.observed = {'status': 'absent'}
        self.calls = 0
        self.fault = None

    async def read(self):
        await asyncio.sleep(0)
        return deepcopy(self.observed)

    async def write(self, intent):
        self.calls += 1
        await asyncio.sleep(0)
        expected = {'if_none_match': '*'} if self.observed['status'] == 'absent' else {'if_match': self.observed['etag']}
        if intent['condition'] != expected or self.fault == 'conflict':
            return {'status': 'conflict'}
        if self.fault == 'not-committed':
            return {'status': 'error'}
        if self.fault == 'cancel':
            raise asyncio.CancelledError()
        self.observed = {'status': 'present', 'etag': f'etag-{self.calls}', 'document': deepcopy(intent['document'])}
        if self.fault == 'lost':
            return {'status': 'error'}
        if self.fault == 'throw-after-commit':
            raise RuntimeError('sensitive transport exception')
        return {'status': 'written', 'etag': self.observed['etag']}

    def coordinator(self):
        return Coordinator(OPTS, {}, self.read, self.write)


@pytest.mark.asyncio
async def test_one_winner_and_no_reattachment():
    store = Store()
    left, right = store.coordinator(), store.coordinator()
    results = await asyncio.gather(left.acquire(), right.acquire(), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    loser = left if isinstance(results[0], Exception) else right
    with pytest.raises(RuntimeError, match='ownership uncertain'):
        await loser.declare([{'count': 1}])
    assert store.calls == 2
    with pytest.raises(ValueError, match='lock held'):
        await store.coordinator().acquire()


@pytest.mark.asyncio
async def test_parallel_attempts_serialize_and_keep_failed_siblings():
    store = Store()
    coordinator = store.coordinator()
    assert await coordinator.snapshot() is None
    await coordinator.acquire()
    await coordinator.declare([{'role': 'broker', 'count': 3}])
    attempts = await asyncio.gather(*(coordinator.start(f'broker-{i}') for i in range(3)))
    assert len(set(attempts)) == 3
    with pytest.raises(ValueError, match='operations outstanding'):
        await coordinator.release()
    await asyncio.gather(coordinator.complete('broker-0', attempts[0]),
                         coordinator.fail('broker-1', attempts[1]),
                         coordinator.complete('broker-2', attempts[2]))
    await coordinator.release()
    result = await coordinator.snapshot()
    assert result['document']['lock'] == {'state': 'idle', 'run_id': None}
    assert [n['phase'] for n in result['document']['nodes'].values()] == ['ready', 'failed', 'ready']
    result['document']['nodes'].clear()
    assert len((await coordinator.snapshot())['document']['nodes']) == 3
    with pytest.raises(ValueError, match='released'):
        await coordinator.start('broker-0')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['lost', 'throw-after-commit'])
async def test_committed_lost_response_is_read_back_without_rewrite(fault):
    store = Store()
    store.fault = fault
    coordinator = store.coordinator()
    await coordinator.acquire()
    assert store.calls == 1
    await coordinator.declare([{}])
    assert store.calls == 2
    assert (await coordinator.snapshot()) == store.observed


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['not-committed', 'conflict'])
async def test_unconfirmed_ownership_poisoned_without_retry_or_release(fault):
    store = Store()
    coordinator = store.coordinator()
    await coordinator.acquire()
    store.fault = fault
    with pytest.raises(RuntimeError, match='ownership uncertain'):
        await coordinator.declare([{}])
    for fn in (coordinator.release, lambda: coordinator.start('0')):
        with pytest.raises(RuntimeError, match='ownership uncertain'):
            await fn()
    assert store.calls == 2
    assert store.observed['document']['lock']['state'] == 'held'


@pytest.mark.asyncio
async def test_validation_and_wrong_local_attempt_do_not_corrupt_ownership():
    store = Store()
    coordinator = store.coordinator()
    with pytest.raises(ValueError, match='not acquired'):
        await coordinator.release()
    await coordinator.acquire()
    with pytest.raises(ValueError, match='already acquired'):
        await coordinator.acquire()
    with pytest.raises(ValueError, match='invalid coordination event'):
        await coordinator.declare([{'count': False}])
    await coordinator.declare([{}])
    operation = await coordinator.start('0')
    with pytest.raises(ValueError, match='local attempt mismatch'):
        await coordinator.complete('0', 'wrong')
    await coordinator.complete('0', operation)
    await coordinator.release()


@pytest.mark.asyncio
async def test_cancellation_keeps_ownership_uncertain():
    store = Store()
    coordinator = store.coordinator()
    await coordinator.acquire()
    store.fault = 'cancel'
    with pytest.raises(asyncio.CancelledError):
        await coordinator.declare([{}])
    with pytest.raises(RuntimeError, match='ownership uncertain'):
        await coordinator.release()
    assert store.observed['document']['lock']['state'] == 'held'


@pytest.mark.asyncio
async def test_reused_ids_refused_before_second_write():
    store = Store()
    coordinator = Coordinator(OPTS, {}, store.read, store.write, lambda: 'same-id')
    with pytest.raises(ValueError, match='invalid coordination id'):
        await coordinator.acquire()
    assert store.calls == 0
