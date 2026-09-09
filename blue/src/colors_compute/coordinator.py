"""One invocation's serialized conditional journal ownership."""
import asyncio
from ._copy import deepcopy
import inspect
import os
from uuid import uuid4

from .contract import _safe
from .coordination import coordination, _nonblank, _shape, _document
from .journal import journal_get, journal_put, _settings, _identity


class Coordinator:
    def __init__(self, opts, environment=None, read=None, write=None, id_factory=None, event_prefix="", reducer=None):
        self._event_prefix = event_prefix
        self._reducer = reducer or coordination
        self._opts = deepcopy(opts)
        self._environment = dict(os.environ if environment is None else environment)
        self._identity = _identity(self._opts, _settings(self._opts))
        self._read = read or (lambda: journal_get(self._opts, self._environment))
        self._write = write or (lambda intent: journal_put(self._opts, intent, self._environment))
        self._factory = id_factory or (lambda: str(uuid4()))
        self._ids = set()
        self._run_id = None
        self._confirmed = None
        self._phase = 'new'
        self._active = {}
        self._mutex = asyncio.Lock()

    def _id(self):
        value = self._factory()
        if not _safe(value) or value in self._ids:
            raise ValueError('invalid coordination id')
        self._ids.add(value)
        return value

    async def _call(self, fn, *args):
        value = fn(*args)
        return await value if inspect.isawaitable(value) else value

    def _uncertain(self):
        self._phase = 'poisoned'
        raise RuntimeError('coordination ownership uncertain') from None

    def _require_acquired(self):
        if self._phase == 'poisoned':
            self._uncertain()
        if self._phase == 'released':
            raise ValueError('coordination released')
        if self._phase != 'acquired':
            raise ValueError('coordination not acquired')

    async def _commit(self, observed, kind, **fields):
        event = {'type': self._event_prefix + kind, 'run_id': self._run_id, 'write_id': self._id(),
                 'target_etag': observed.get('etag') if isinstance(observed, dict) else None, **fields}
        # Reducer errors happen before I/O and preserve confirmed ownership.
        intent = self._reducer(observed, self._identity, event)
        try:
            result = await self._call(self._write, deepcopy(intent))
        except asyncio.CancelledError:
            self._phase = 'poisoned'
            raise
        except Exception:
            result = {'status': 'error'}
        if _shape(result, ('status', 'etag')) and result['status'] == 'written' and _nonblank(result['etag']):
            confirmed = {'status': 'present', 'etag': result['etag'], 'document': deepcopy(intent['document'])}
        elif result == {'status': 'error'}:
            try:
                confirmed = await self._call(self._read)
            except asyncio.CancelledError:
                self._phase = 'poisoned'
                raise
            except Exception:
                self._uncertain()
            if not (_shape(confirmed, ('status', 'etag', 'document'))
                    and confirmed['status'] == 'present' and _nonblank(confirmed['etag'])
                    and _document(confirmed['document']) and confirmed['document'] == intent['document']):
                self._uncertain()
        else:
            self._uncertain()
        self._confirmed = deepcopy(confirmed)
        return deepcopy(confirmed)

    async def acquire(self):
        async with self._mutex:
            if self._phase == 'poisoned':
                self._uncertain()
            if self._phase == 'released':
                raise ValueError('coordination released')
            if self._phase != 'new':
                raise ValueError('coordination already acquired')
            self._phase = 'acquiring'
            self._run_id = self._id()
            try:
                observed = await self._call(self._read)
            except asyncio.CancelledError:
                self._phase = 'poisoned'
                raise
            except Exception:
                self._uncertain()
            if observed == {'status': 'error'}:
                self._uncertain()
            result = await self._commit(observed, 'acquire')
            self._phase = 'acquired'
            return result

    async def declare(self, topology):
        async with self._mutex:
            self._require_acquired()
            return await self._commit(self._confirmed, 'declare', topology=deepcopy(topology))

    async def transition(self, suffix, **fields):
        if suffix not in ('key-intent', 'key-prepared', 'key-cleanup', 'key-removed',
                          'begin-delete', 'retire', 'recreate', 'retry', 'shared-retry'):
            raise ValueError('invalid coordination transition')
        async with self._mutex:
            self._require_acquired()
            return await self._commit(self._confirmed, suffix, **deepcopy(fields))

    async def _begin(self, kind, node_id):
        async with self._mutex:
            self._require_acquired()
            operation = self._id()
            await self._commit(self._confirmed, kind, node_id=node_id, operation_id=operation)
            self._active[node_id] = operation
            return operation

    async def start(self, node_id):
        return await self._begin('start', node_id)

    async def destroy(self, node_id):
        return await self._begin('destroy', node_id)

    async def _shared_begin(self, kind):
        async with self._mutex:
            self._require_acquired()
            operation = self._id()
            await self._commit(self._confirmed, kind, operation_id=operation)
            self._active['$shared'] = operation
            return operation

    async def shared_start(self):
        return await self._shared_begin('shared-start')

    async def shared_destroy(self):
        return await self._shared_begin('shared-destroy')

    async def shared_complete(self, operation_id):
        return await self._finish('shared-complete', '$shared', operation_id)

    async def shared_fail(self, operation_id):
        return await self._finish('shared-fail', '$shared', operation_id)

    async def _finish(self, kind, node_id, operation_id):
        async with self._mutex:
            if self._active.get(node_id) != operation_id or node_id not in self._active:
                raise ValueError('coordination local attempt mismatch')
            del self._active[node_id]
            self._require_acquired()
            fields = {'operation_id': operation_id}
            if node_id != '$shared':
                fields['node_id'] = node_id
            return await self._commit(self._confirmed, kind, **fields)

    async def complete(self, node_id, operation_id):
        return await self._finish('complete', node_id, operation_id)

    async def fail(self, node_id, operation_id):
        return await self._finish('fail', node_id, operation_id)

    async def release(self):
        async with self._mutex:
            self._require_acquired()
            if self._active:
                raise ValueError('coordination operations outstanding')
            result = await self._commit(self._confirmed, 'release')
            self._phase = 'released'
            return result

    async def snapshot(self):
        async with self._mutex:
            return deepcopy(self._confirmed)
