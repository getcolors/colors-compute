"""Journal intentions must preserve ownership, strict schemas, and caller inputs."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from colors_compute.coordination import coordination

IDENTITY = {'profile': 'demo', 'provider': 'vultr', 'backend': {'kind': 's3', 'bucket': 'states', 'region': 'eu-west-1'}}


def event(kind, write='write-1', etag=None, **extra):
    return {'type': kind, 'run_id': 'run-1', 'write_id': write, 'target_etag': etag, **extra}


def observe(document):
    return {'status': 'present', 'etag': 'etag', 'document': document}


def acquired():
    return coordination({'status': 'absent'}, IDENTITY, event('acquire'))['document']


def declared():
    return coordination(observe(acquired()), IDENTITY,
                        event('declare', 'write-2', 'etag', topology=[{'role': 'broker', 'count': 2}]))['document']


@pytest.mark.parametrize('case', json.loads((Path(__file__).resolve().parents[2] / 'test/fixtures/coordination.json').read_text()), ids=lambda case: case['name'])
def test_common_cases(case):
    try:
        result = coordination(*case['args'])
    except ValueError as error:
        result = {'error': str(error)}
    assert result == case['expected']


def test_complete_intentions_do_not_mutate_or_alias_inputs():
    doc = declared()
    obs = observe(doc)
    request = event('start', 'write-3', 'etag', node_id='broker-0', operation_id='operation-1')
    before = deepcopy((obs, IDENTITY, request))
    result = coordination(obs, IDENTITY, request)
    assert (obs, IDENTITY, request) == before
    result['document']['identity']['backend']['bucket'] = 'changed'
    result['document']['nodes']['broker-1']['phase'] = 'changed'
    assert (obs, IDENTITY, request) == before
    fresh_identity = deepcopy(IDENTITY)
    first = coordination({'status': 'absent'}, fresh_identity, event('acquire'))
    first['document']['identity']['backend']['bucket'] = 'changed'
    assert fresh_identity == IDENTITY


@pytest.mark.parametrize('target', ['document', 'identity', 'backend', 'lock', 'node'])
def test_unknown_nested_secret_keys_rejected(target):
    document = declared()
    locations = {'document': document, 'identity': document['identity'],
                 'backend': document['identity']['backend'], 'lock': document['lock'],
                 'node': document['nodes']['broker-0']}
    locations[target]['password'] = 'do-not-echo'
    with pytest.raises(ValueError, match='^invalid coordination document$'):
        coordination(observe(document), IDENTITY, event('release', 'write-3', 'etag'))


def test_integral_numbers_and_boolean_rejection():
    doc = acquired()
    doc['revision'] = 1.0
    doc['schema_version'] = 1.0
    result = coordination(observe(doc), IDENTITY, event('declare', 'write-2', 'etag', topology=[{'count': 1.0}]))
    assert result['document']['nodes']['0']['index'] == 0
    for number in (True, 1.5, float('inf'), 10**400):
        with pytest.raises(ValueError, match='^invalid coordination event$'):
            coordination(observe(doc), IDENTITY, event('declare', 'write-2', 'etag', topology=[{'count': number}]))


def test_running_release_refuses_and_failed_outcome_retained():
    running = coordination(observe(declared()), IDENTITY,
        event('start', 'write-3', 'etag', node_id='broker-0', operation_id='operation-1'))['document']
    with pytest.raises(ValueError, match='^coordination operations outstanding$'):
        coordination(observe(running), IDENTITY, event('release', 'write-4', 'etag'))
    failed = coordination(observe(running), IDENTITY,
        event('fail', 'write-4', 'etag', node_id='broker-0', operation_id='operation-1'))['document']
    released = coordination(observe(failed), IDENTITY, event('release', 'write-5', 'etag'))['document']
    assert released['nodes']['broker-0']['phase'] == 'failed'
    assert released['nodes']['broker-1']['phase'] == 'declared'
    assert released['lock'] == {'state': 'idle', 'run_id': None}
