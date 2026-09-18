import asyncio
from copy import deepcopy

import pytest

from colors_compute.coordinator import Coordinator
from colors_compute.orchestration import orchestrate
from test_coordinator import Store, OPTS


class Runtime:
    def __init__(self):
        self.store = Store()
        self.states = {}
        self.events = []
        self.fail = set()
        self.prepared = False
    def coordinator(self, opts, env, **kwargs):
        return Coordinator(opts, env, self.store.read, self.store.write, **kwargs)
    async def prepare(self, opts, ownership, env, intent, prepared):
        if ownership['status'] == 'fresh':
            await intent()
            await prepared('SHA256:' + 'A' * 43)
        self.prepared = True
        return {'mode': 'managed', 'public_key': 'ssh-ed25519 public', 'fingerprint': 'SHA256:' + 'A' * 43}
    async def cleanup(self, *args):
        self.events.append('cleanup')
        self.prepared = False
        return {'mode': 'managed', 'cleaned': True}
    def assemble(self, opts, topology, request, key):
        nodes = self.store.observed['document']['nodes']
        return {'shared': {'node_id': 'shared'}, 'nodes': [{'node_id': n} for n in nodes if nodes[n]['desired']]}
    def render(self, opts, stage, request, shared=None):
        return {'documents': {'test.tf.json': {'node_id': request['node_id'] if stage == 'node' else 'shared'}}}
    async def presence(self, opts, key, env):
        return {'status': 'present' if key in self.states else 'absent'}
    async def read(self, opts, key, env, include_outputs=False):
        return {'status': 'present', 'params': {'provider': 'vultr'}, 'outputs': self.states[key]} if key in self.states else {'status': 'error'}
    async def converge(self, opts, key, docs, operation, presence, env):
        node = docs['test.tf.json']['node_id']
        self.events.append(operation + ':' + node)
        await asyncio.sleep(.001)
        if node in self.fail:
            self.events.append('failed:' + node)
            return {'status': 'error'}
        if operation == 'delete':
            self.states.pop(key, None)
            return {'status': 'destroyed'}
        params = {'node_id': node, 'provider': 'vultr', 'name': 'demo-' + node, 'ip': '192.0.2.1', 'user': 'root', 'sudoer': 'root'}
        outputs = {'params': params}
        self.states[key] = outputs
        self.events.append('ready:' + node)
        return {'status': 'ready', 'params': params, 'outputs': outputs}
    def dependencies(self):
        return {'runtime_preflight': lambda *_: [], 'coordinator': self.coordinator, 'prepare_keypair': self.prepare, 'cleanup_keypair': self.cleanup,
                'validate_deployment': lambda *_: True,
                'compute_credential_errors': lambda *_: [],
                'registration_preflight': lambda *args, **kwargs: {'status': 'checked'}, 'deployment_requests': self.assemble, 'provider_request': self.render,
                'state_presence': self.presence, 'read_state': self.read, 'converge_state': self.converge}
    async def run(self, count=2, event='create'):
        return await orchestrate({**OPTS, 'blue/event': event, 'compute-prevent-destroy': False}, [{'count': count}], {}, {}, self.dependencies())


@pytest.mark.asyncio
async def test_real_sdk_create_scaledown_delete_and_recreate():
    runtime = Runtime()
    result = await runtime.run()
    assert result['status'] == 'ready'
    assert [n['node_id'] for n in result['cluster']['nodes']] == ['0', '1']
    assert runtime.events.index('ready:shared') < runtime.events.index('create:0')
    runtime.events.clear()
    assert (await runtime.run(count=1))['status'] == 'ready'
    assert runtime.events.index('delete:1') < runtime.events.index('create:shared')
    runtime.events.clear()
    assert (await runtime.run(count=1, event='delete')) == {'status': 'destroyed'}
    assert runtime.events == ['delete:0', 'delete:shared', 'cleanup']
    assert not runtime.states and not runtime.prepared
    assert runtime.store.observed['document']['status'] == 'retired'
    assert (await runtime.run(count=1))['status'] == 'ready'
    assert runtime.store.observed['document']['generation'] == 2


@pytest.mark.asyncio
async def test_failed_siblings_settle_and_absent_failed_creation_refuses_retry():
    runtime = Runtime()
    runtime.fail.add('0')
    assert await runtime.run() == {'status': 'error'}
    assert 'ready:1' in runtime.events
    doc = runtime.store.observed['document']
    assert doc['lock']['state'] == 'idle'
    assert doc['nodes']['0']['phase'] == 'failed'
    runtime.events.clear()
    runtime.fail.clear()
    assert (await runtime.run())['diagnostics'][0]['code'] == 'failed-operation-without-state'
    assert 'create:0' not in runtime.events


@pytest.mark.asyncio
async def test_unowned_remote_state_refused_before_compute_and_lost_release_errors():
    runtime = Runtime()
    runtime.states['demo/compute/shared.tfstate'] = {'params': {'provider': 'vultr'}}
    assert await runtime.run() == {'status': 'error'}
    assert runtime.events == []


@pytest.mark.asyncio
async def test_protected_delete_and_build_perform_no_io():
    runtime = Runtime()
    for opts in ({**OPTS, 'blue/event': 'delete'}, {**OPTS, 'blue/event': 'build'}):
        assert await orchestrate(opts, [{'count': 1}], {}, {}, runtime.dependencies()) == {'status': 'error'}
    assert runtime.store.calls == 0


@pytest.mark.asyncio
async def test_actual_ssh_keygen_callbacks_and_cleanup_with_native_coordinator(tmp_path):
    runtime = Runtime()
    dependencies = runtime.dependencies()
    del dependencies['prepare_keypair']
    del dependencies['cleanup_keypair']
    opts = {**OPTS, 'compute-prevent-destroy': False, 'blue/event': 'create'}
    result = await orchestrate(opts, [{'count': 1}], {}, {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}, dependencies)
    assert result['status'] == 'ready'
    private = tmp_path / '.ssh/demo'
    assert private.is_file() and private.stat().st_mode & 0o777 == 0o600
    assert runtime.store.observed['document']['key']['fingerprint'].startswith('SHA256:')
    assert await orchestrate({**opts, 'blue/event': 'delete'}, [{'count': 1}], {}, {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}, dependencies) == {'status': 'destroyed'}
    assert not private.exists() and not private.with_suffix('.pub').exists()


@pytest.mark.asyncio
async def test_unowned_new_node_state_prevents_local_key_generation(tmp_path):
    runtime = Runtime()
    runtime.states['demo/compute/nodes/0.tfstate'] = {'params': {'provider': 'vultr'}}
    dependencies = runtime.dependencies()
    del dependencies['prepare_keypair']
    assert await orchestrate(OPTS, [{'count': 1}], {}, {'HOME': str(tmp_path)}, dependencies) == {'status': 'error'}
    assert not (tmp_path / '.ssh').exists()


@pytest.mark.asyncio
async def test_cancellation_settles_started_siblings_then_propagates():
    runtime = Runtime()
    ready = asyncio.Event()
    finish = asyncio.Event()
    dependencies = runtime.dependencies()
    original = runtime.converge
    async def delayed(opts, key, docs, operation, presence, env):
        if '/nodes/' in key:
            ready.set()
            await finish.wait()
        return await original(opts, key, docs, operation, presence, env)
    dependencies['converge_state'] = delayed
    task = asyncio.create_task(orchestrate(OPTS, [{'count': 2}], {}, {}, dependencies))
    await ready.wait()
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.store.observed['document']['lock']['state'] == 'idle'
    assert all(node['phase'] == 'ready' for node in runtime.store.observed['document']['nodes'].values())


@pytest.mark.asyncio
async def test_recreate_accepts_only_confirmed_empty_remaining_state_objects():
    runtime = Runtime()
    assert (await runtime.run(count=1))['status'] == 'ready'
    assert (await runtime.run(count=1, event='delete'))['status'] == 'destroyed'
    runtime.states = {'demo/compute/shared.tfstate': {}, 'demo/compute/nodes/0.tfstate': {}}
    dependencies = runtime.dependencies()
    original = runtime.read
    async def read(opts, key, env, include_outputs=False):
        if key in runtime.states and runtime.states[key] == {}:
            return {'status': 'present', 'params': {}, 'outputs': {}, 'state_empty': True}
        return await original(opts, key, env, include_outputs)
    dependencies['read_state'] = read
    result = await orchestrate({**OPTS, 'compute-prevent-destroy': False}, [{'count': 1}], {}, {}, dependencies)
    assert result['status'] == 'ready'
    assert runtime.store.observed['document']['generation'] == 2


@pytest.mark.asyncio
async def test_invalid_application_requirements_refuse_before_real_key_generation(tmp_path):
    runtime = Runtime()
    dependencies = runtime.dependencies()
    del dependencies['validate_deployment']
    del dependencies['prepare_keypair']
    result = await orchestrate(OPTS, [{'count': 1}], {}, {'HOME': str(tmp_path)}, dependencies)
    assert result == {'status': 'error'}
    assert not (tmp_path / '.ssh').exists()
    assert runtime.events == []


@pytest.mark.asyncio
async def test_missing_credentials_follow_ownership_checks_and_precede_key_generation():
    runtime = Runtime()
    dependencies = runtime.dependencies()
    del dependencies['compute_credential_errors']
    result = await orchestrate(OPTS, [{'count': 1}], {}, {}, dependencies)
    assert result == {'status': 'error', 'errors': [
        'required credential is not set: COLORS_PAR_VULTR_API_KEY']}
    assert runtime.store.calls > 0
    assert not runtime.prepared and runtime.events == []
    runtime = Runtime()
    runtime.states['demo/compute/shared.tfstate'] = {'params': {'provider': 'vultr'}}
    dependencies = runtime.dependencies()
    del dependencies['compute_credential_errors']
    assert await orchestrate(OPTS, [{'count': 1}], {}, {}, dependencies) == {'status': 'error'}
    assert not runtime.prepared and runtime.events == []

@pytest.mark.asyncio
async def test_existing_state_guard_refuses_fresh_and_retired_without_writes():
    runtime = Runtime()
    guarded = {**OPTS, 'compute-require-existing-state': True}
    async def run():
        return await orchestrate(guarded, [{'count': 2}], {}, {}, runtime.dependencies())
    assert await run() == {'status': 'error'}
    assert runtime.store.calls == 0 and runtime.events == [] and not runtime.prepared
    assert (await runtime.run())['status'] == 'ready'
    assert (await run())['status'] == 'ready'
    assert await runtime.run(event='delete') == {'status': 'destroyed'}
    before = runtime.store.calls
    runtime.events.clear()
    assert await run() == {'status': 'error'}
    assert runtime.store.calls == before and runtime.events == []
    assert await orchestrate({**guarded, 'blue/event': 'delete', 'compute-prevent-destroy': False},
                             [{'count': 2}], {}, {}, runtime.dependencies()) == {'status': 'destroyed'}

@pytest.mark.asyncio
async def test_existing_state_guard_refuses_uninitialized_and_invalid_options():
    from colors_compute.contract import validate
    for value in (None, 'true', 1, []):
        runtime = Runtime()
        opts = {**OPTS, 'compute-require-existing-state': value}
        assert ':compute-require-existing-state must be a boolean' in validate(opts)
        assert await orchestrate(opts, [{'count': 1}], {}, {}, runtime.dependencies()) == {'status': 'error'}
        assert runtime.store.calls == 0
    runtime = Runtime()
    owner = runtime.coordinator(OPTS, {}, event_prefix='lifecycle/')
    await owner.acquire()
    await owner.release()
    before = runtime.store.calls
    assert await orchestrate({**OPTS, 'compute-require-existing-state': True}, [{'count': 1}], {}, {}, runtime.dependencies()) == {'status': 'error'}
    assert runtime.store.calls == before

@pytest.mark.asyncio
@pytest.mark.parametrize('empty', [True, False, None])
async def test_recovered_declared_empty_nodes_delete_without_convergence(empty):
    from colors_compute.inspection import read_deployment
    runtime = Runtime()
    assert (await runtime.run())['status'] == 'ready'
    for node in runtime.store.observed['document']['nodes'].values():
        node.update(phase='declared', operation=None, operation_id=None)
    deps = runtime.dependencies()
    original = deps['read_state']
    async def read(opts, key, env, include_outputs=False):
        if '/nodes/' in key:
            return {'status': 'present', 'state_empty': empty}
        return await original(opts, key, env, include_outputs)
    deps['read_state'] = read
    reader = {'journal_get': lambda *_: runtime.store.observed, 'read_state': read}
    assert await read_deployment(OPTS, {}, reader) == {'status': 'partial' if empty is True else 'error'}
    runtime.events.clear()
    result = await orchestrate({**OPTS, 'blue/event': 'delete', 'compute-prevent-destroy': False}, [{'count': 2}], {}, {}, deps)
    assert result == {'status': 'destroyed' if empty is True else 'error'}
    assert not any(event.startswith('delete:0') or event.startswith('delete:1') for event in runtime.events)

@pytest.mark.asyncio
async def test_retired_journal_held_by_finalizer_reports_destroyed():
    from colors_compute.inspection import read_deployment
    runtime = Runtime()
    assert (await runtime.run())['status'] == 'ready'
    assert (await runtime.run(event='delete'))['status'] == 'destroyed'
    runtime.store.observed['document']['lock'] = {'state': 'held', 'run_id': 'finalizer'}
    assert await read_deployment(OPTS, {}, {'journal_get': lambda *_: runtime.store.observed}) == {'status': 'destroyed'}

@pytest.mark.asyncio
@pytest.mark.parametrize('empty', [True, False, None])
async def test_delete_before_key_preparation_retires_only_verified_empty_states(empty):
    runtime = Runtime()
    deps = runtime.dependencies()
    deps['validate_deployment'] = lambda *_: False
    assert await orchestrate(OPTS, [{'count': 1}], {}, {}, deps) == {'status': 'error'}
    assert runtime.store.observed['document']['key']['phase'] == 'absent'
    runtime.states['demo/compute/nodes/0.tfstate'] = {}
    deps['read_state'] = lambda *args, **kwargs: {'status': 'present', 'state_empty': empty}
    def forbidden(*args, **kwargs):
        raise AssertionError('delete must not prepare keys, render or require compute credentials')
    for name in ('prepare_keypair', 'cleanup_keypair', 'provider_request', 'compute_credential_errors'):
        deps[name] = forbidden
    result = await orchestrate({**OPTS, 'blue/event': 'delete', 'compute-prevent-destroy': False}, [{'count': 1}], {}, {}, deps)
    assert result == {'status': 'destroyed' if empty is True else 'error'}
    assert runtime.store.observed['document']['lock']['state'] == 'idle'
    if empty is True:
        doc = runtime.store.observed['document']
        assert doc['status'] == 'retired' and doc['key']['phase'] == 'absent'
        assert doc['shared']['phase'] == doc['nodes']['0']['phase'] == 'destroyed'
        runtime.states.clear()
        assert (await runtime.run(count=1))['status'] == 'ready'
        assert runtime.store.observed['document']['generation'] == 2

@pytest.mark.asyncio
@pytest.mark.parametrize('remaining', ['none', 'private', 'public', 'foreign', 'new-home'])
async def test_delete_without_complete_managed_keypair(tmp_path, remaining):
    runtime = Runtime()
    deps = runtime.dependencies()
    deps.pop('prepare_keypair')
    deps.pop('cleanup_keypair')
    env = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    assert (await orchestrate(OPTS, [{'count': 1}], {}, env, deps))['status'] == 'ready'
    private, public = tmp_path / '.ssh/demo', tmp_path / '.ssh/demo.pub'
    if remaining not in ('private', 'foreign'):
        private.unlink()
    if remaining != 'public':
        public.unlink()
    if remaining == 'foreign':
        private.write_text('foreign key file')
    delete_env = {**env, 'HOME': str(tmp_path / 'other-machine')} if remaining == 'new-home' else env
    result = await orchestrate({**OPTS, 'blue/event': 'delete', 'compute-prevent-destroy': False}, [{'count': 1}], {}, delete_env, deps)
    assert not runtime.states
    if remaining == 'foreign':
        assert result == {'status': 'error'} and private.read_text() == 'foreign key file'
        assert runtime.store.observed['document']['lock']['state'] == 'held'
    else:
        assert result == {'status': 'destroyed'}
        assert not private.exists() and not public.exists()

@pytest.mark.asyncio
async def test_delete_resumes_after_key_removed_before_retire():
    runtime = Runtime()
    assert (await runtime.run(count=1))['status'] == 'ready'
    assert (await runtime.run(count=1, event='delete'))['status'] == 'destroyed'
    runtime.store.observed['document']['status'] = 'deleting'
    runtime.store.observed['document']['lock'] = {'state': 'held', 'run_id': 'interrupted-owner'}
    assert (await runtime.run(count=1, event='delete')) == {'status': 'error'}
    assert runtime.store.observed['document']['lock']['run_id'] == 'interrupted-owner'
    runtime.store.observed['document']['lock'] = {'state': 'idle', 'run_id': None}
    deps = runtime.dependencies()
    def forbidden(*args, **kwargs):
        raise AssertionError('retirement must not touch keys or compute')
    for name in ('prepare_keypair', 'cleanup_keypair', 'compute_credential_errors', 'provider_request'):
        deps[name] = forbidden
    assert await orchestrate({**OPTS, 'blue/event': 'delete', 'compute-prevent-destroy': False}, [{'count': 1}], {}, {}, deps) == {'status': 'destroyed'}
    assert runtime.store.observed['document']['status'] == 'retired'
    assert runtime.store.observed['document']['lock']['state'] == 'idle'


@pytest.mark.asyncio
async def test_missing_tools_fail_before_backend_or_ownership(tmp_path):
    runtime = Runtime()
    deps = runtime.dependencies()
    deps.pop('runtime_preflight')
    def never(*_):
        raise AssertionError('must not touch backend or ownership')
    deps.update(bootstrap_backend=never, coordinator=never)
    result = await orchestrate(OPTS, [{'count': 1}], {}, {'PATH': str(tmp_path)}, deps)
    assert result['diagnostics'][0]['code'] == 'missing-tool'
    assert result['diagnostics'][0]['tools'] == ['aws', 'ssh-keygen', 'tofu']
    assert 'load the deployment environment' in result['errors'][0]
    assert runtime.store.observed == {'status': 'absent'}


@pytest.mark.asyncio
async def test_state_diagnostics_and_redacted_unexpected_errors():
    for presence, code in [({'status': 'error', 'secret': 'PRIVATE'}, 'state-unreadable'),
                           ({'status': 'absent'}, 'recorded-state-missing')]:
        runtime = Runtime()
        assert (await runtime.run())['status'] == 'ready'
        runtime.events.clear()
        deps = runtime.dependencies()
        deps['state_presence'] = lambda *_: presence
        result = await orchestrate(OPTS, [{'count': 2}], {}, {}, deps)
        assert result['diagnostics'][0]['code'] == code
        assert 'PRIVATE' not in str(result)
        assert runtime.events == []
        assert runtime.store.observed['document']['lock']['state'] == 'idle'
    runtime = Runtime()
    def broken(*_):
        raise ValueError('PRIVATE provider output')
    result = await orchestrate(OPTS, [{'count': 1}], {}, {}, {**runtime.dependencies(), 'bootstrap_backend': broken})
    assert result == {'status': 'error'}


def test_tool_lookup_requires_executable_file_in_supplied_path(tmp_path):
    from colors_compute.diagnostics import missing_tools
    opts = {**OPTS, 'provider-backend': 'local', 'vultr-ssh-keys': ['external']}
    assert missing_tools(opts, {}) == ['tofu']
    tool = tmp_path / 'tofu'
    tool.mkdir()
    assert missing_tools(opts, {'PATH': str(tmp_path)}) == ['tofu']
    tool.rmdir()
    tool.write_text('#!/bin/sh\nexit 0\n')
    tool.chmod(0o600)
    assert missing_tools(opts, {'PATH': str(tmp_path)}) == ['tofu']
    tool.chmod(0o700)
    assert missing_tools(opts, {'PATH': str(tmp_path)}) == []


@pytest.mark.asyncio
async def test_node_diagnostic_survives_fanout_and_siblings_settle():
    runtime = Runtime()
    reads = 0
    async def presence(opts, key, env):
        nonlocal reads
        if key.endswith('/nodes/0.tfstate'):
            reads += 1
            if reads == 2:
                return {'status': 'error'}
        return await runtime.presence(opts, key, env)
    result = await orchestrate(OPTS, [{'count': 2}], {}, {}, {**runtime.dependencies(), 'state_presence': presence})
    assert result['diagnostics'][0]['code'] == 'state-unreadable'
    assert 'ready:1' in runtime.events
    assert 'create:0' not in runtime.events
    assert runtime.store.observed['document']['lock']['state'] == 'idle'
