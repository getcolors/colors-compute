import pytest
from colors_compute.deployment_request import deployment_requests

OPTS = {'profile': 'demo', 'provider-compute': 'vultr'}
KEY = {'mode': 'managed', 'public_key': 'public', 'private_key_path': '/private'}
REQUIREMENTS = {'security': {'ingress': [], 'egress': 'all', 'private_filter': False}}


def test_cluster_names_remain_stable_when_scaled():
    small = deployment_requests(OPTS, [{'count': 1}], REQUIREMENTS, KEY)
    large = deployment_requests(OPTS, [{'count': 3}], REQUIREMENTS, KEY)
    assert small['nodes'][0] == large['nodes'][0]
    assert small['nodes'][0]['name'] == 'demo-0'
    assert small['shared']['network']['mode'] == 'created'
    assert 'private_key_path' not in small['shared']['key']


def test_single_host_and_role_names():
    assert deployment_requests(OPTS, [{'count': 1}], {**REQUIREMENTS, 'single_host': True}, KEY)['nodes'][0]['name'] == 'demo'
    result = deployment_requests({**OPTS, 'vultr-name': 'custom'}, [{'role': 'server', 'count': 1}], REQUIREMENTS, KEY)
    assert result['nodes'][0]['name'] == 'custom-server-0'
    assert result['shared']['name'] == 'custom'
    with pytest.raises(ValueError, match='topology'):
        deployment_requests(OPTS, [{'count': 2}], {**REQUIREMENTS, 'single_host': True}, KEY)


def test_no_input_mutation_or_silent_truncation():
    result = deployment_requests(OPTS, [{'count': 1}], REQUIREMENTS, KEY)
    result['nodes'][0]['security']['ingress'].append('changed')
    assert REQUIREMENTS['security']['ingress'] == []
    assert result['shared']['security']['ingress'] == []
    with pytest.raises(ValueError, match='derived compute name'):
        deployment_requests({**OPTS, 'vultr-name': 'x' * 63}, [{'count': 1}], REQUIREMENTS, KEY)
