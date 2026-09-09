"""Deployment-wide journal ownership around native Colors compute fan-out."""
import asyncio
from ._copy import deepcopy
import inspect
import os

from blue.workflow import run
from .backend import read_state
from .contract import state_keys
from .coordination import _topology
from .coordinator import Coordinator
from .execution import state_presence, converge_state
from .provider_request import provider_request
from .key_request import key_request
from .registration import registration_preflight
from .ssh import prepare_keypair, cleanup_keypair, _mode
from .workflow import cluster_workflow
from .planning import validate_deployment


async def orchestrate(opts, topology, request, environment=None, dependencies=None):
    """Return ready cluster, destroyed, or generic error. Never emit raw state."""
    deps = dependencies or {}
    env = dict(os.environ if environment is None else environment)
    opts, topology, request = deepcopy(opts), deepcopy(topology), deepcopy(request)
    coordinator, acquired, keys = None, False, None

    async def call(name, default, *args, **kwargs):
        value = deps.get(name, default)(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    def require(condition):
        if not condition:
            raise ValueError('compute lifecycle refused')

    async def snapshot():
        return (await coordinator.snapshot())['document']

    async def readable(key):
        result = await call('read_state', read_state, opts, key, env, include_outputs=True)
        require(result.get('status') == 'present' and result.get('params', {}).get('provider') == opts['provider-compute'])
        return result

    async def presence_for(key, record):
        presence = await call('state_presence', state_presence, opts, key, env)
        require(presence in ({'status': 'present'}, {'status': 'absent'}))
        if record['phase'] in ('declared', 'destroyed'):
            if presence == {'status': 'present'}:
                empty = await call('read_state', read_state, opts, key, env, include_outputs=True)
                require(empty.get('status') == 'present' and empty.get('state_empty') is True)
        elif record['phase'] != 'destroyed':
            require(presence == {'status': 'present'})
        if record['phase'] == 'failed':
            await readable(key)
        return presence

    async def attempt(node_id, documents, operation):
        doc = await snapshot()
        record = doc['shared'] if node_id is None else doc['nodes'][node_id]
        key = keys['shared'] if node_id is None else state_keys(opts['profile'], [node_id])['nodes'][node_id]
        if operation == 'delete' and record['phase'] == 'destroyed':
            return {'status': 'destroyed'}
        presence = await presence_for(key, record)
        if operation == 'create' and record['phase'] == 'failed':
            require(record['operation'] == 'create')
            fields = {'evidence': 'readable-state'}
            if node_id is not None:
                fields['node_id'] = node_id
            await coordinator.transition('shared-retry' if node_id is None else 'retry', **fields)
        if node_id is None:
            operation_id = await (coordinator.shared_start() if operation == 'create' else coordinator.shared_destroy())
        else:
            operation_id = await (coordinator.start(node_id) if operation == 'create' else coordinator.destroy(node_id))
        try:
            result = {'status': 'destroyed'} if operation == 'delete' and record['phase'] == 'declared' and presence == {'status': 'absent'} else await call('converge_state', converge_state, opts, key, documents, operation, presence, env)
        except BaseException:
            if node_id is None:
                await coordinator.shared_fail(operation_id)
            else:
                await coordinator.fail(node_id, operation_id)
            raise
        success = result.get('status') == ('ready' if operation == 'create' else 'destroyed')
        if node_id is None:
            await (coordinator.shared_complete(operation_id) if success else coordinator.shared_fail(operation_id))
        else:
            await (coordinator.complete(node_id, operation_id) if success else coordinator.fail(node_id, operation_id))
        require(success)
        return result

    async def execute():
        nonlocal coordinator, acquired, keys
        declarations = _topology(topology)
        require(declarations is not None and opts.get('blue/event', 'create') in ('create', 'delete') and opts.get('blue/dry-run') is not True)
        if request.get('private') is True:
            declarations = [{**node, 'private': True} for node in declarations]
        legacy_keys = request.get('legacy_state_keys', [])
        require(isinstance(legacy_keys, list) and len(legacy_keys) == len(set(legacy_keys)))
        for legacy_key in legacy_keys:
            observed = await call('state_presence', state_presence, opts, legacy_key, env, legacy=True)
            require(observed == {'status': 'absent'})
        operation = opts.get('blue/event', 'create')
        require(operation != 'delete' or opts.get('compute-prevent-destroy') is False)
        keys = state_keys(opts['profile'], [node['node_id'] for node in declarations])
        coordinator = deps.get('coordinator', Coordinator)(opts, env, event_prefix='lifecycle/')
        await coordinator.acquire()
        acquired = True
        doc = await snapshot()
        if operation == 'delete' and doc['status'] == 'retired':
            return {'status': 'destroyed'}
        if operation == 'create' and doc['status'] == 'retired':
            await coordinator.transition('recreate')
            doc = await snapshot()
        require(doc['status'] == 'active' if operation == 'create' else doc['status'] in ('active', 'deleting'))
        selected = _mode(opts)
        require(doc['key']['mode'] in (None, selected['mode']))
        shared_read = None
        # Read every recorded state before touching local key files or compute.
        for node_id, record in [(None, doc['shared']), *doc['nodes'].items()]:
            state_key = keys['shared'] if node_id is None else state_keys(opts['profile'], [node_id])['nodes'][node_id]
            observed = await presence_for(state_key, record)
            if observed == {'status': 'present'} and record['phase'] not in ('declared', 'destroyed'):
                read_result = await readable(state_key)
                if node_id is None:
                    shared_read = read_result
        if operation == 'create':
            for node in declarations:
                if node['node_id'] not in doc['nodes']:
                    await presence_for(keys['nodes'][node['node_id']], {'phase': 'declared'})
            await coordinator.declare(topology)
        else:
            await coordinator.transition('begin-delete')
        doc = await snapshot()
        if operation == 'create':
            require(await call('validate_deployment', validate_deployment, opts, topology, request) is True)
            ownership_registration = (shared_read or {}).get('outputs', {}).get('registration')
            await call('registration_preflight', registration_preflight, opts, selected['mode'], ownership_registration, environment=env)
        key_record = doc['key']
        if operation == 'delete' and key_record['phase'] == 'absent':
            # No successful preparation means no owned private key/resources.
            require(doc['shared']['phase'] == 'declared' and all(n['phase'] == 'declared' for n in doc['nodes'].values()))
        require(key_record['phase'] in ('absent', 'prepared'))
        ownership = {'status': 'prepared', 'fingerprint': key_record['fingerprint']} if key_record['mode'] == 'managed' else {'status': 'fresh'}
        async def record_intent():
            await coordinator.transition('key-intent', mode='managed')
            return True
        async def record_prepared(fingerprint):
            await coordinator.transition('key-prepared', fingerprint=fingerprint)
            return True
        if operation == 'create':
            key = await call('prepare_keypair', prepare_keypair, opts, ownership, env, record_intent, record_prepared)
            if key['mode'] == 'external' and key_record['phase'] == 'absent':
                await coordinator.transition('key-intent', mode='external')
                await coordinator.transition('key-prepared', fingerprint=None)
        else:
            # Verify existing key without generation. Delete uses create semantics
            # solely for read-only verification of a recorded prepared keypair.
            require(key_record['phase'] == 'prepared')
            key = await call('prepare_keypair', prepare_keypair, {**opts, 'blue/event': 'create'}, ownership, env,
                             record_intent, record_prepared)
        normalized_key = await call('key_request', key_request, opts, key, env)
        if 'deployment_requests' in deps:
            assembly = await call('deployment_requests', None, opts, topology, request, normalized_key)
        else:
            from .deployment_request import deployment_requests
            assembly = deployment_requests(opts, topology, request, normalized_key)
        shared_request = assembly['shared']
        shared_plan = await call('provider_request', provider_request, opts, 'shared', shared_request)
        doc = await snapshot()
        existing_shared = {}
        if any(node['phase'] != 'destroyed' and (operation == 'delete' or not node['desired']) for node in doc['nodes'].values()):
            if doc['shared']['phase'] != 'declared':
                existing_shared = (await readable(keys['shared']))['outputs']
        for node_id, node in doc['nodes'].items():
            if node['phase'] == 'destroyed' or operation == 'create' and node['desired']:
                continue
            if node['phase'] == 'declared':
                await attempt(node_id, {}, 'delete')
                continue
            node_request = deepcopy(shared_request)
            node_request['node_id'] = node_id
            node_request.pop('name', None)
            plan = await call('provider_request', provider_request, opts, 'node', node_request, existing_shared)
            await attempt(node_id, plan['documents'], 'delete')
        if operation == 'delete':
            await attempt(None, shared_plan['documents'], 'delete')
            await coordinator.transition('key-cleanup')
            await call('cleanup_keypair', cleanup_keypair, opts, ownership, {'all_resources_destroyed': True}, env)
            await coordinator.transition('key-removed')
            await coordinator.transition('retire')
            return {'status': 'destroyed'}
        shared = await attempt(None, shared_plan['documents'], 'create')
        shared_outputs = shared['outputs']
        requests = {node['node_id']: node for node in assembly['nodes']}
        async def node_step(values):
            node_id = values['colors-compute/request']['node_id']
            try:
                plan = await call('provider_request', provider_request, opts, 'node', requests[node_id], shared_outputs)
                result = await attempt(node_id, plan['documents'], 'create')
                params = {**result['params']}
                if key.get('private_key_path'):
                    params['ssh_identity_file'] = key['private_key_path']
                return {**values, 'colors-compute/params': params}
            except Exception:
                return {**values, 'blue/exit': 1, 'blue/err': 'compute node failed'}
        task = asyncio.create_task(call('run', run, cluster_workflow(declarations, declarations[0]['node_id'], node_step), opts))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        require(result.get('blue/exit') == 0 and 'colors-compute/cluster' in result)
        return {'status': 'ready', 'cluster': result['colors-compute/cluster'], 'shared': shared_outputs, 'key': {k: v for k, v in key.items() if k in ('mode', 'private_key_path', 'fingerprint')}}
    cancelled = None
    try:
        result = await execute()
    except asyncio.CancelledError as error:
        cancelled = error
        result = {'status': 'error'}
    except Exception:
        result = {'status': 'error'}
    if acquired:
        try:
            await coordinator.release()
        except asyncio.CancelledError as error:
            cancelled = error
            result = {'status': 'error'}
        except Exception:
            result = {'status': 'error'}
    if cancelled is not None:
        raise cancelled
    return result
