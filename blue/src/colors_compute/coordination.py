"""Pure conditional journal-write intentions; no transport or mutation authority."""
from ._copy import deepcopy
import math
import re

from .contract import _safe, expand, registry, state_keys

MAX_INTEGER = 9007199254740991


def _integer(value, minimum, maximum):
    return (type(value) in (int, float) and minimum <= value <= maximum
            and math.isfinite(value) and value == int(value))


def _shape(value, fields):
    return isinstance(value, dict) and set(value) == set(fields)


def _nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def _role(value):
    return value is None or isinstance(value, str) and bool(re.fullmatch(r'[a-z][a-z0-9]*(-[a-z0-9]+)*', value))


def _identity(value):
    if not _shape(value, ('profile', 'provider', 'backend')) or not _safe(value['profile']):
        return False
    if not isinstance(value['provider'], str) or value['provider'] not in registry()['compute']:
        return False
    backend = value['backend']
    if not isinstance(backend, dict):
        return False
    kind = backend.get('kind')
    if kind not in ('r2', 's3') or not _shape(backend, ('kind', 'bucket', 'region', 'endpoint') if kind == 'r2' else ('kind', 'bucket', 'region')):
        return False
    if not isinstance(backend['bucket'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', backend['bucket']) or not _safe(backend['region']):
        return False
    return kind == 's3' or (backend['region'] == 'auto' and isinstance(backend['endpoint'], str)
                           and bool(re.fullmatch(r'https://[a-zA-Z0-9.-]+(:[0-9]{1,5})?/?', backend['endpoint'])))


def _topology(value):
    if not isinstance(value, list) or not value:
        return None
    normalized, total = [], 0
    for declaration in value:
        if not isinstance(declaration, dict) or set(declaration) - {'role', 'count'}:
            return None
        count = declaration.get('count', 1)
        if not _role(declaration.get('role')) or not _integer(count, 1, 1000):
            return None
        total += int(count)
        if total > 1000:
            return None
        normalized.append({'role': declaration.get('role'), 'count': int(count)})
    try:
        return expand(normalized)
    except ValueError:
        return None


def _event(value):
    if not isinstance(value, dict):
        return False
    kind = value.get('type')
    extra = {'acquire': (), 'declare': ('topology',), 'start': ('node_id', 'operation_id'),
             'complete': ('node_id', 'operation_id'), 'fail': ('node_id', 'operation_id'), 'release': ()}
    if not isinstance(kind, str) or kind not in extra or not _shape(value, ('type', 'run_id', 'write_id', 'target_etag', *extra[kind])):
        return False
    if not _safe(value['run_id']) or not _safe(value['write_id']) or not (value['target_etag'] is None or _nonblank(value['target_etag'])):
        return False
    if kind == 'declare':
        return _topology(value['topology']) is not None
    if kind in ('start', 'complete', 'fail'):
        return _safe(value['node_id']) and _safe(value['operation_id'])
    return True


def _observation(value):
    if not isinstance(value, dict):
        return False
    status = value.get('status')
    if status in ('absent', 'error'):
        return _shape(value, ('status',))
    return status == 'present' and _shape(value, ('status', 'etag', 'document')) and _nonblank(value['etag'])


def _document_v1(value):
    if not _shape(value, ('schema_version', 'identity', 'revision', 'write_id', 'lock', 'topology_declared', 'nodes')):
        return False
    if not _integer(value['schema_version'], 1, 1) or not _identity(value['identity']) or not _integer(value['revision'], 1, MAX_INTEGER) or not _safe(value['write_id']):
        return False
    lock = value['lock']
    if not _shape(lock, ('state', 'run_id')) or not ((lock['state'] == 'held' and _safe(lock['run_id'])) or (lock['state'] == 'idle' and lock['run_id'] is None)):
        return False
    nodes = value['nodes']
    declared = value['topology_declared']
    if type(declared) is not bool or not isinstance(nodes, dict) or len(nodes) > 1000 or declared != bool(nodes):
        return False
    roles, operations = {}, set()
    for node_id, node in nodes.items():
        if not _safe(node_id) or not _shape(node, ('state_key', 'role', 'index', 'phase', 'operation_id')):
            return False
        role, index, phase, operation = node['role'], node['index'], node['phase'], node['operation_id']
        if not _role(role) or not _integer(index, 0, 999):
            return False
        expected_id = f'{role}-{int(index)}' if role is not None else str(int(index))
        if node_id != expected_id or node['state_key'] != state_keys(value['identity']['profile'], [node_id])['nodes'][node_id]:
            return False
        if phase not in ('declared', 'running', 'ready', 'failed'):
            return False
        if phase == 'declared':
            if operation is not None:
                return False
        elif not _safe(operation) or operation in operations:
            return False
        else:
            operations.add(operation)
        if phase == 'running' and lock['state'] != 'held':
            return False
        roles.setdefault(role, []).append(int(index))
    if None in roles and len(roles) > 1:
        return False
    return all(sorted(indices) == list(range(len(indices))) for indices in roles.values())


def _document(value):
    if isinstance(value, dict) and value.get('schema_version') == 2:
        from .lifecycle import lifecycle_document_valid
        return lifecycle_document_valid(value)
    return _document_v1(value)


def coordination(observation, identity, event):
    """Return a new conditional-write intent; all inputs remain unchanged."""
    if isinstance(event, dict) and isinstance(event.get('type'), str) and event['type'].startswith('lifecycle/'):
        from .lifecycle import lifecycle
        return lifecycle(observation, identity, event)
    def fail(message):
        raise ValueError(message)
    if not _identity(identity):
        fail('invalid coordination identity')
    if not _event(event):
        fail('invalid coordination event')
    if not _observation(observation):
        fail('invalid coordination observation')
    if observation['status'] == 'error':
        fail('coordination read failed')
    present = observation['status'] == 'present'
    document = observation.get('document')
    if present:
        if not _document_v1(document):
            fail('invalid coordination document')
        if document['identity'] != identity:
            fail('coordination identity mismatch')
    if event['target_etag'] != (observation['etag'] if present else None):
        fail('stale coordination observation')
    if present and event['write_id'] == document['write_id']:
        fail('coordination write_id reused')
    if present and document['revision'] == MAX_INTEGER:
        fail('coordination revision exhausted')
    kind = event['type']
    if not present:
        if kind != 'acquire':
            fail('coordination object absent')
        return {'condition': {'if_none_match': '*'}, 'document': {
            'schema_version': 1, 'identity': deepcopy(identity), 'revision': 1,
            'write_id': event['write_id'], 'lock': {'state': 'held', 'run_id': event['run_id']},
            'topology_declared': False, 'nodes': {}}}
    result = deepcopy(document)
    if kind == 'acquire':
        if document['lock']['state'] == 'held':
            fail('coordination lock held')
        result['lock'] = {'state': 'held', 'run_id': event['run_id']}
    else:
        if document['lock']['state'] != 'held':
            fail('coordination lock not held')
        if document['lock']['run_id'] != event['run_id']:
            fail('coordination owner mismatch')
        if kind == 'declare':
            if document['topology_declared']:
                fail('coordination topology already declared')
            nodes = _topology(event['topology'])
            keys = state_keys(identity['profile'], [node['node_id'] for node in nodes])['nodes']
            result['nodes'] = {node['node_id']: {'state_key': keys[node['node_id']], 'role': node['role'],
                'index': node['index'], 'phase': 'declared', 'operation_id': None} for node in nodes}
            result['topology_declared'] = True
        elif kind in ('start', 'complete', 'fail'):
            if not document['topology_declared']:
                fail('coordination topology not declared')
            node_id = event['node_id']
            if node_id not in document['nodes']:
                fail('coordination node undeclared')
            node = document['nodes'][node_id]
            if kind == 'start':
                if node['phase'] != 'declared':
                    fail('coordination node not startable')
                if any(n['operation_id'] == event['operation_id'] for n in document['nodes'].values()):
                    fail('coordination operation_id reused')
                result['nodes'][node_id].update(phase='running', operation_id=event['operation_id'])
            else:
                if node['phase'] != 'running':
                    fail('coordination node not running')
                if node['operation_id'] != event['operation_id']:
                    fail('coordination operation mismatch')
                result['nodes'][node_id]['phase'] = 'ready' if kind == 'complete' else 'failed'
        elif kind == 'release':
            if any(node['phase'] == 'running' for node in document['nodes'].values()):
                fail('coordination operations outstanding')
            result['lock'] = {'state': 'idle', 'run_id': None}
    result['revision'] = int(document['revision']) + 1
    result['write_id'] = event['write_id']
    return {'condition': {'if_match': observation['etag']}, 'document': result}
