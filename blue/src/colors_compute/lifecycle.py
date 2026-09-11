"""Strict schema-two lifecycle journal intentions; no I/O or dispatch authority."""
from ._copy import deepcopy
import re

from .contract import _safe, state_keys
from .coordination import _identity, _shape, _nonblank, _integer, _role, _topology, _observation, MAX_INTEGER


def _fingerprint(value):
    return isinstance(value, str) and bool(re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value))


def _running(record):
    return record['phase'] in ('running', 'destroying')


def _idle_shared():
    return {'phase': 'declared', 'operation': None, 'operation_id': None}


def _record(record):
    phase = record['phase']
    if phase not in ('declared', 'running', 'ready', 'failed', 'destroying', 'destroyed'):
        return False
    if phase == 'declared':
        return record['operation'] is None and record['operation_id'] is None
    if not _safe(record['operation_id']) or record['operation'] not in ('create', 'destroy'):
        return False
    if phase in ('running', 'ready'):
        return record['operation'] == 'create'
    if phase in ('destroying', 'destroyed'):
        return record['operation'] == 'destroy'
    return True


def lifecycle_document_valid(value):
    if not _shape(value, ('schema_version', 'identity', 'revision', 'write_id', 'lock', 'generation', 'status', 'topology_declared', 'key', 'shared', 'nodes')):
        return False
    if not _integer(value['schema_version'], 2, 2) or not _identity(value['identity']) or not _integer(value['revision'], 1, MAX_INTEGER) or not _integer(value['generation'], 1, MAX_INTEGER) or not _safe(value['write_id']) or value['status'] not in ('active', 'deleting', 'retired') or type(value['topology_declared']) is not bool:
        return False
    lock = value['lock']
    if not _shape(lock, ('state', 'run_id')) or not ((lock['state'] == 'held' and _safe(lock['run_id'])) or (lock['state'] == 'idle' and lock['run_id'] is None)):
        return False
    key = value['key']
    if not _shape(key, ('mode', 'phase', 'fingerprint')) or key['phase'] not in ('absent', 'intent', 'prepared', 'cleanup', 'removed'):
        return False
    if key['phase'] == 'absent':
        if key['mode'] is not None or key['fingerprint'] is not None:
            return False
    else:
        if key['mode'] not in ('managed', 'external'):
            return False
        if key['mode'] == 'external' or key['phase'] == 'intent':
            if key['fingerprint'] is not None:
                return False
        elif not _fingerprint(key['fingerprint']):
            return False
    if not _shape(value['shared'], ('phase', 'operation', 'operation_id')) or not _record(value['shared']) or not isinstance(value['nodes'], dict) or len(value['nodes']) > 1000:
        return False
    operations = set()
    if value['shared']['operation_id'] is not None:
        operations.add(value['shared']['operation_id'])
    if _running(value['shared']) and lock['state'] != 'held':
        return False
    desired_roles = {}
    for node_id, node in value['nodes'].items():
        if not _safe(node_id) or not _shape(node, ('state_key', 'role', 'index', 'desired', 'phase', 'operation', 'operation_id')) or not _role(node['role']) or not _integer(node['index'], 0, 999) or type(node['desired']) is not bool or not _record(node):
            return False
        expected_id = str(int(node['index'])) if node['role'] is None else f"{node['role']}-{int(node['index'])}"
        if node_id != expected_id or node['state_key'] != f"{value['identity']['profile']}/compute/nodes/{node_id}.tfstate":
            return False
        if node['operation_id'] is not None:
            if node['operation_id'] in operations:
                return False
            operations.add(node['operation_id'])
        if _running(node) and lock['state'] != 'held':
            return False
        if node['desired']:
            desired_roles.setdefault(node['role'], []).append(int(node['index']))
    if None in desired_roles and len(desired_roles) != 1:
        return False
    destroyed = value['shared']['phase'] == 'destroyed' and all(node['phase'] == 'destroyed' for node in value['nodes'].values())
    undesired = all(not node['desired'] for node in value['nodes'].values())
    if value['status'] == 'retired' and not (key['phase'] == 'removed' and destroyed and undesired):
        return False
    if value['status'] == 'deleting' and not undesired:
        return False
    if key['phase'] in ('cleanup', 'removed') and not (value['status'] in ('deleting', 'retired') and destroyed):
        return False
    return all(sorted(indices) == list(range(len(indices))) for indices in desired_roles.values())


EXTRAS = {'acquire': (), 'release': (), 'begin-delete': (), 'retire': (), 'recreate': (),
          'declare': ('topology',), 'key-intent': ('mode',), 'key-prepared': ('fingerprint',),
          'key-cleanup': (), 'key-removed': (), 'shared-start': ('operation_id',),
          'shared-destroy': ('operation_id',), 'shared-complete': ('operation_id',),
          'shared-fail': ('operation_id',), 'shared-retry': ('evidence',),
          'start': ('node_id', 'operation_id'), 'destroy': ('node_id', 'operation_id'),
          'complete': ('node_id', 'operation_id'), 'fail': ('node_id', 'operation_id'),
          'retry': ('node_id', 'evidence')}


def _event(value):
    if not isinstance(value, dict) or not isinstance(value.get('type'), str) or not value['type'].startswith('lifecycle/'):
        return False
    name = value['type'][10:]
    if name not in EXTRAS or not _shape(value, ('type', 'run_id', 'write_id', 'target_etag', *EXTRAS[name])) or not _safe(value['run_id']) or not _safe(value['write_id']) or not (value['target_etag'] is None or _nonblank(value['target_etag'])):
        return False
    if 'node_id' in value and not _safe(value['node_id']):
        return False
    if 'operation_id' in value and not _safe(value['operation_id']):
        return False
    if name == 'declare' and _topology(value['topology']) is None:
        return False
    if name == 'key-intent' and value['mode'] not in ('managed', 'external'):
        return False
    if name == 'key-prepared' and value['fingerprint'] is not None and not _fingerprint(value['fingerprint']):
        return False
    return 'evidence' not in value or value['evidence'] == 'readable-state' or (name in ('shared-retry', 'retry') and value['evidence'] == 'verified-provider-absence')


def lifecycle(observation, identity, event):
    def fail(message):
        raise ValueError(message)
    def require(condition):
        if not condition:
            fail('lifecycle transition refused')
    if not _identity(identity):
        fail('invalid lifecycle identity')
    if not _event(event):
        fail('invalid lifecycle event')
    if not _observation(observation):
        fail('invalid lifecycle observation')
    if observation['status'] == 'error':
        fail('lifecycle read failed')
    present, doc, name = observation['status'] == 'present', observation.get('document'), event['type'][10:]
    if present and not lifecycle_document_valid(doc):
        fail('invalid lifecycle document')
    if present and doc['identity'] != identity:
        fail('lifecycle identity mismatch')
    if event['target_etag'] != (observation['etag'] if present else None):
        fail('lifecycle stale observation')
    if present and event['write_id'] == doc['write_id']:
        fail('lifecycle write_id reused')
    if present and doc['revision'] == MAX_INTEGER:
        fail('lifecycle revision exhausted')
    if not present:
        require(name == 'acquire')
        return {'condition': {'if_none_match': '*'}, 'document': {
            'schema_version': 2, 'identity': deepcopy(identity), 'revision': 1, 'write_id': event['write_id'],
            'lock': {'state': 'held', 'run_id': event['run_id']}, 'generation': 1, 'status': 'active',
            'topology_declared': False, 'key': {'mode': None, 'phase': 'absent', 'fingerprint': None},
            'shared': _idle_shared(), 'nodes': {}}}
    result = deepcopy(doc)
    def records():
        return [result['shared'], *result['nodes'].values()]
    def active():
        return any(_running(record) for record in records())
    def destroyed():
        return all(record['phase'] == 'destroyed' for record in records())
    def unique():
        return all(record['operation_id'] != event.get('operation_id') for record in records())
    if name == 'acquire':
        if result['lock']['state'] == 'held':
            fail('lifecycle lock held')
        result['lock'] = {'state': 'held', 'run_id': event['run_id']}
    else:
        if result['lock']['state'] != 'held':
            fail('lifecycle lock not held')
        if result['lock']['run_id'] != event['run_id']:
            fail('lifecycle owner mismatch')
        if name == 'declare':
            require(result['status'] == 'active' and not active())
            requested = _topology(event['topology'])
            require(len(set(result['nodes']) | {node['node_id'] for node in requested}) <= 1000)
            for node in result['nodes'].values():
                node['desired'] = False
            keys = state_keys(identity['profile'], [node['node_id'] for node in requested])
            for node in requested:
                old = result['nodes'].get(node['node_id'])
                result['nodes'][node['node_id']] = {**old, 'desired': True} if old and old['phase'] != 'destroyed' else {
                    'state_key': keys['nodes'][node['node_id']], 'role': node['role'], 'index': node['index'], 'desired': True, **_idle_shared()}
            result['topology_declared'] = True
        elif name == 'key-intent':
            require(result['status'] == 'active' and result['key']['phase'] == 'absent')
            result['key'] = {'mode': event['mode'], 'phase': 'intent', 'fingerprint': None}
        elif name == 'key-prepared':
            require(result['key']['phase'] == 'intent' and (_fingerprint(event['fingerprint']) if result['key']['mode'] == 'managed' else event['fingerprint'] is None))
            result['key'].update(phase='prepared', fingerprint=event['fingerprint'])
        elif name == 'shared-start':
            require(result['status'] == 'active' and result['topology_declared'] and result['key']['phase'] == 'prepared' and not active() and result['shared']['phase'] in ('declared', 'ready') and unique())
            result['shared'] = {'phase': 'running', 'operation': 'create', 'operation_id': event['operation_id']}
        elif name == 'shared-destroy':
            require(all(node['phase'] == 'destroyed' for node in result['nodes'].values()) and result['shared']['phase'] not in ('running', 'destroying', 'destroyed') and unique())
            result['shared'] = {'phase': 'destroying', 'operation': 'destroy', 'operation_id': event['operation_id']}
        elif name in ('shared-complete', 'shared-fail'):
            require(_running(result['shared']) and result['shared']['operation_id'] == event['operation_id'])
            result['shared']['phase'] = 'failed' if name == 'shared-fail' else 'ready' if result['shared']['operation'] == 'create' else 'destroyed'
        elif name == 'shared-retry':
            require(result['shared']['phase'] == 'failed' and result['shared']['operation'] == 'create')
            result['shared'] = _idle_shared()
        elif name in ('start', 'destroy'):
            require(event['node_id'] in result['nodes'] and unique())
            node = result['nodes'][event['node_id']]
            if name == 'start':
                require(result['status'] == 'active' and node['desired'] and result['key']['phase'] == 'prepared' and result['shared']['phase'] == 'ready' and node['phase'] in ('declared', 'ready'))
            else:
                require(not node['desired'] and node['phase'] not in ('running', 'destroying', 'destroyed'))
            node.update(phase='running' if name == 'start' else 'destroying', operation='create' if name == 'start' else 'destroy', operation_id=event['operation_id'])
        elif name in ('complete', 'fail'):
            require(event['node_id'] in result['nodes'])
            node = result['nodes'][event['node_id']]
            require(_running(node) and node['operation_id'] == event['operation_id'])
            node['phase'] = 'failed' if name == 'fail' else 'ready' if node['operation'] == 'create' else 'destroyed'
        elif name == 'retry':
            require(event['node_id'] in result['nodes'])
            node = result['nodes'][event['node_id']]
            require(node['phase'] == 'failed' and node['operation'] == 'create')
            node.update(_idle_shared())
        elif name == 'begin-delete':
            require(result['status'] != 'retired' and not active())
            result['status'] = 'deleting'
            for node in result['nodes'].values():
                node['desired'] = False
        elif name == 'key-cleanup':
            require(result['status'] == 'deleting' and destroyed() and result['key']['phase'] == 'prepared')
            result['key']['phase'] = 'cleanup'
        elif name == 'key-removed':
            require(result['key']['phase'] == 'cleanup')
            result['key']['phase'] = 'removed'
        elif name == 'retire':
            require(result['status'] == 'deleting' and result['key']['phase'] == 'removed' and destroyed())
            result['status'] = 'retired'
        elif name == 'recreate':
            require(result['status'] == 'retired' and not active() and result['generation'] < MAX_INTEGER)
            result.update(generation=int(result['generation']) + 1, status='active', key={'mode': None, 'phase': 'absent', 'fingerprint': None}, shared=_idle_shared(), topology_declared=False)
        elif name == 'release':
            require(not active() and result['key']['phase'] not in ('intent', 'cleanup'))
            result['lock'] = {'state': 'idle', 'run_id': None}
        else:
            require(False)
    result['revision'] = int(result['revision']) + 1
    result['write_id'] = event['write_id']
    return {'condition': {'if_match': observation['etag']}, 'document': result}
