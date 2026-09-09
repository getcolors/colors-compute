"""Serialized zero-change plans for an existing, fully converged deployment."""
import asyncio
import inspect
import os
import stat
from pathlib import Path
from ._copy import deepcopy
from .contract import collect, expand, state_keys, compute_credential_errors
from .coordinator import Coordinator
from .journal import journal_get, journal_put
from .lifecycle import lifecycle_document_valid
from .backend import read_state
from .execution import check_state
from .ssh import _mode, _fingerprint
from .key_request import key_request
from .deployment_request import deployment_requests
from .provider_request import provider_request


def _public_key(opts, fingerprint, environment):
    directory = Path(environment.get('HOME') or str(Path.home())) / '.ssh'
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('invalid SSH public key directory')
    path = directory / (opts['profile'] + '.pub')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('invalid SSH public key file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError('invalid SSH public key file')
        value = data.decode('utf-8').strip()
        if _fingerprint(value) != fingerprint:
            raise ValueError('SSH public key fingerprint differs from ownership')
        return value
    finally:
        os.close(fd)


async def check_deployment_drift(opts, topology, requirements, environment=None, dependencies=None):
    """Never applies or prepares keys. The only remote writes acquire/release the journal."""
    opts, topology, requirements = deepcopy(opts), deepcopy(topology), deepcopy(requirements)
    env, deps = dict(os.environ if environment is None else environment), dependencies or {}
    owner, acquired, cancelled = None, False, None
    async def call(name, default, *args, **kwargs):
        value = deps.get(name, default)(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value
    def require(value):
        if not value:
            raise ValueError('compute drift refused')
    declarations = None
    def validate_document(doc):
        require(lifecycle_document_valid(doc) and doc['status'] == 'active' and doc['topology_declared'] and doc['shared']['phase'] == 'ready' and doc['key']['phase'] == 'prepared')
        require(all(not record['desired'] for record in doc['nodes'].values() if record['phase'] == 'destroyed'))
        active = {id: record for id, record in doc['nodes'].items() if record['phase'] != 'destroyed'}
        require(set(active) == {node['node_id'] for node in declarations})
        for node in declarations:
            record = active[node['node_id']]
            require(record['phase'] == 'ready' and record['desired'] and record['role'] == node['role'] and record['index'] == node['index'])
    async def read_existing():
        result = await call('journal_get', journal_get, opts, env)
        require(result.get('status') == 'present')
        return result
    async def execute():
        nonlocal owner, acquired, declarations
        require(not opts.get('blue/dry-run') and opts.get('blue/event') != 'build')
        declarations = expand(topology)
        if requirements.get('private') is True:
            declarations = [{**node, 'private': True} for node in declarations]
        require(declarations and len(declarations) <= 1000)
        observed = await read_existing()
        validate_document(observed['document'])
        require(observed['document']['lock']['state'] == 'idle')
        owner = Coordinator(opts, environment=env, read=read_existing, write=lambda intent: call('journal_put', journal_put, opts, intent, env), event_prefix='lifecycle/')
        await owner.acquire()
        acquired = True
        doc = (await owner.snapshot())['document']
        validate_document(doc)
        keys = state_keys(opts['profile'], [node['node_id'] for node in declarations])
        results = []
        shared = await call('read_state', read_state, opts, keys['shared'], env, include_outputs=True)
        require(shared.get('status') == 'present' and shared.get('params', {}).get('provider') == opts['provider-compute'])
        for node in declarations:
            result = await call('read_state', read_state, opts, keys['nodes'][node['node_id']], env)
            require(result.get('status') == 'present')
            results.append(result.get('params', {}))
        cluster = collect(declarations, results, requirements.get('entry_node_id', declarations[0]['node_id']))
        errors = await call('compute_credential_errors', compute_credential_errors, opts, env)
        if errors:
            return {'status': 'error', 'errors': errors}
        selected = _mode(opts)
        require(selected['mode'] == doc['key']['mode'])
        if selected['mode'] == 'managed':
            selected['public_key'] = await call('public_key', _public_key, opts, doc['key']['fingerprint'], env)
        public = await call('key_request', key_request, opts, selected, env)
        assembly = deployment_requests(opts, topology, requirements, public)
        if 'roles' in assembly['shared']:
            assembly['shared']['peers'] = {n['node_id']: {'role': n['role'], 'vpc_ip': n['vpc_ip']} for n in cluster['nodes']}
        plans = [(keys['shared'], provider_request(opts, 'shared', assembly['shared'])['documents'])]
        plans.extend((keys['nodes'][node['node_id']], provider_request(opts, 'node', node, shared['outputs'])['documents']) for node in assembly['nodes'])
        for key, documents in plans:
            require(await call('check_state', check_state, opts, key, documents, env) == {'status': 'clean'})
        return {'status': 'clean'}
    try:
        result = await execute()
    except asyncio.CancelledError as error:
        cancelled, result = error, {'status': 'error'}
    except Exception:
        result = {'status': 'error'}
    if acquired:
        try:
            await owner.release()
        except asyncio.CancelledError as error:
            cancelled, result = error, {'status': 'error'}
        except Exception:
            result = {'status': 'error'}
    if cancelled:
        raise cancelled
    return result
