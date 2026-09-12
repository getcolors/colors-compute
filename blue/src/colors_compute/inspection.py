"""Read a deployment's recorded node inventory without changing ownership."""
import inspect
import os
from pathlib import Path

from .backend import read_state
from .contract import collect, state_keys
from .journal import journal_get, _identity, _settings
from .lifecycle import lifecycle_document_valid
from .managed_backend import backend_presence
from .ssh import _mode


async def read_deployment(opts, environment=None, dependencies=None, requirements=None):
    deps = dependencies or {}
    env = dict(os.environ if environment is None else environment)
    async def call(name, default, *args, **kwargs):
        value = deps.get(name, default)(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value
    try:
        requirements = {} if requirements is None else requirements
        if not isinstance(requirements, dict):
            return {"status": "error"}
        observed = await call('journal_get', journal_get, opts, env)
        if observed.get('status') != 'present' and observed != {'status': 'absent'}:
            # A managed bucket that no longer exists is a retired deployment, not a transport error.
            if await call('backend_presence', backend_presence, opts, env) == {'status': 'absent'}:
                observed = {'status': 'absent'}
        if observed == {'status': 'absent'}:
            return {'status': 'absent'}
        if observed.get('status') != 'present':
            return {'status': 'error'}
        document = observed.get('document')
        if not lifecycle_document_valid(document) or document['identity'] != _identity(opts, _settings(opts)):
            return {'status': 'error'}
        if document['status'] == 'retired':
            return {'status': 'destroyed'}
        if document['lock']['state'] != 'idle':
            return {'status': 'error'}
        shared = await call('read_state', read_state, opts, state_keys(opts['profile'], [])['shared'], env, include_outputs=True)
        if shared.get('status') != 'present' or shared.get('params', {}).get('provider') != opts['provider-compute']:
            return {'status': 'error'}
        records = [node for node in document['nodes'].values() if node['phase'] != 'destroyed']
        if records and all(node['phase'] == 'declared' for node in records):
            if document['shared']['phase'] != 'ready' or document['key']['phase'] != 'prepared' or _mode(opts)['mode'] != document['key']['mode']:
                return {'status': 'error'}
            for node in records:
                state = await call('read_state', read_state, opts, node['state_key'], env, include_outputs=True)
                if state != {'status': 'absent'} and not (state.get('status') == 'present' and state.get('state_empty') is True):
                    return {'status': 'error'}
            return {'status': 'partial'}
        declarations, results = [], []
        for node_id, node in document['nodes'].items():
            if node['phase'] == 'destroyed':
                continue
            if node['phase'] not in ('ready', 'failed'):
                return {'status': 'error'}
            state = await call('read_state', read_state, opts, node['state_key'], env)
            if state.get('status') != 'present':
                return {'status': 'error'}
            declarations.append({'node_id': node_id, 'role': node['role'], 'index': node['index'], 'provider': opts['provider-compute']})
            results.append(state.get('params', {}))
        if not declarations:
            return {'status': 'error'}
        declarations.sort(key=lambda node: (node['role'] or '', node['index']))
        entry = requirements.get('entry_node_id', declarations[0]['node_id'])
        if not isinstance(entry, str) or entry not in {node['node_id'] for node in declarations}:
            return {'status': 'error'}
        cluster = collect(declarations, results, entry)
        key = {'mode': document['key']['mode']}
        selected = _mode(opts)
        if selected['mode'] != key['mode']:
            return {'status': 'error'}
        if key['mode'] == 'managed':
            key['private_key_path'] = str(Path(env.get('HOME') or str(Path.home())) / '.ssh' / opts['profile'])
        elif selected.get('private_key_path'):
            key['private_key_path'] = selected['private_key_path']
        if key.get('private_key_path'):
            for node in cluster['nodes']:
                node['ssh_identity_file'] = key['private_key_path']
        return {'status': 'present', 'cluster': cluster, 'shared': shared['outputs'], 'key': key}
    except Exception:
        return {'status': 'error'}
