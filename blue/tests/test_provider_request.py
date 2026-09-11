from copy import deepcopy
import json
from pathlib import Path

import pytest

from colors_compute.provider_request import provider_request

ROOT = Path(__file__).resolve().parents[2]
CASES = [dict(name=c['name'], opts=c['args'][0], stage=c['args'][1], request=c['args'][2], shared=c['args'][3], expected=c['expected']) for c in json.loads((ROOT / 'test/fixtures/provider-requests.json').read_text())]


@pytest.mark.parametrize('case', CASES, ids=lambda item: item['name'])
def test_all_eight_provider_stages(case):
    original = deepcopy(case)
    if 'error' in case['expected']:
        with pytest.raises(ValueError) as error:
            provider_request(case['opts'], case['stage'], case['request'], case['shared'])
        assert str(error.value) == case['expected']['error']
        assert case == original
        return
    result = provider_request(case['opts'], case['stage'], case['request'], case['shared'])
    assert result == case['expected']
    assert '{{' not in json.dumps(result['documents'])
    assert result['provider'] == case['opts']['provider-compute']
    result['inputs'].clear()
    result['documents'].clear()
    assert case == original


def sample(name='vultr-shared'):
    return deepcopy(next(case for case in CASES if case['name'] == name))


def run(case):
    return provider_request(case['opts'], case['stage'], case['request'], case['shared'])


def test_recipe_distribution_matches_canonical():
    assert (ROOT / 'contracts/provider-recipes.json').read_bytes() == (ROOT / 'blue/src/colors_compute/provider-recipes.json').read_bytes()


def test_vultr_security_rule_port_and_source_mapping():
    result = run(sample())
    ssh = result['inputs']['ingress']['ssh:192.0.2.1/32']
    peer = result['inputs']['ingress']['peer:private']
    assert ssh == {'protocol': 'tcp', 'port': '22', 'ip_type': 'v4', 'subnet': '192.0.2.1', 'subnet_size': 32}
    assert peer['port'] == '9093-9094' and peer['subnet'] == '10.42.0.0' and peer['subnet_size'] == 16


def test_digitalocean_private_sources_remain_discovered():
    result = run(sample('digitalocean-shared'))
    assert result['inputs']['private_ingress'] == {'peer:private': {'protocol': 'tcp', 'port_range': '9093-9094'}}
    assert result['inputs']['public_ingress']['ssh:192.0.2.1/32']['source_addresses'] == ['192.0.2.1/32']


def test_shared_references_feed_nodes_not_package_resources():
    case = sample('vultr-node')
    case['shared']['params']['vpc_id'] = 'existing-network'
    case['shared']['ssh_key_id'] = 'existing-key'
    result = run(case)
    assert result['inputs']['vpc_ids'] == ['existing-network']
    assert result['inputs']['ssh_key_ids'] == ['existing-key']
    assert set(result['documents']['node.tf.json']['resource']) == {'vultr_instance'}


def test_google_firewall_names_stay_stable_when_rules_added():
    case = sample('google-shared')
    initial = run(case)['inputs']['ingress']
    case['request']['security']['ingress'].append({'id': 'aaa', 'protocol': 'udp', 'from_port': 53, 'to_port': 53, 'sources': ['192.0.2.2/32']})
    changed = run(case)['inputs']['ingress']
    for key, rule in initial.items():
        assert changed[key] == rule


def test_external_key_mode_and_oci_image_stage_selection():
    case = sample('vultr-node')
    case['request']['key'] = {'mode': 'external', 'ids': ['operator-key']}
    assert run(case)['inputs']['ssh_key_ids'] == ['operator-key']
    case = sample('vultr-shared')
    case['request']['key'] = {'mode': 'external', 'ids': ['operator-key']}
    assert run(case)['stage'] == 'shared'
    case = sample('oci-node')
    case['opts'].pop('oci-image-id')
    result = run(case)
    assert result['stage'] == 'node-discovery'
    assert 'node-image-discovery.tf.json' in result['documents']


@pytest.mark.parametrize('mutation,message', [
    ('unknown', 'invalid compute request'), ('network', 'unsupported compute network mode'),
    ('sources', 'invalid compute ingress'), ('cidr', 'invalid compute network CIDR'),
    ('egress', 'unsupported compute security policy'), ('ssh', 'compute public SSH ingress required'),
    ('port', 'invalid compute ingress'), ('private', 'unsupported compute private filtering'),
])
def test_invalid_and_unsupported_requirements_fail(mutation, message):
    case = sample('hcloud-shared' if mutation == 'private' else 'vultr-shared')
    request = case['request']
    if mutation == 'unknown': request['secret'] = 'do-not-echo'
    if mutation == 'network': request['network']['mode'] = 'unsupported'
    if mutation == 'sources': request['security']['ingress'][0]['sources'] *= 2
    if mutation == 'cidr': request['network']['cidr'] = '10.42.0.1/16'
    if mutation == 'egress': request['security']['egress'] = 'restricted'
    if mutation == 'ssh': request['security']['ingress'][0]['from_port'] = 23; request['security']['ingress'][0]['to_port'] = 23
    if mutation == 'port': request['security']['ingress'][0]['from_port'] = True
    if mutation == 'private': request['security']['private_filter'] = True
    with pytest.raises(ValueError, match='^' + message + '$'):
        run(case)


def test_missing_settings_and_shared_provider_refuse():
    case = sample('vultr-node')
    case['opts'].pop('vultr-plan')
    with pytest.raises(ValueError, match='^missing compute input: opts.vultr-plan$'):
        run(case)
    case = sample('vultr-node')
    case['shared']['params']['provider'] = 'aws'
    with pytest.raises(ValueError, match='^compute shared provider mismatch$'):
        run(case)


def test_user_values_cannot_inject_terraform_expressions():
    case = sample('vultr-shared')
    case['request']['key']['public_key'] = 'ssh-ed25519 ${file("/private")} fixture'
    with pytest.raises(ValueError, match='^invalid compute literal$'):
        run(case)
    case = sample('vultr-node')
    case['shared']['params']['firewall_group_id'] = '%{ if true }unsafe'
    with pytest.raises(ValueError, match='^invalid compute literal$'):
        run(case)


def test_private_address_type_and_integral_key_reference():
    case = sample('hcloud-node')
    case['request']['network']['private_ip'] = 170524682
    with pytest.raises(ValueError, match='^invalid compute private address$'):
        run(case)
    case = sample('vultr-node')
    case['request']['key'] = {'mode': 'external', 'ids': [123.0]}
    assert run(case)['inputs']['ssh_key_ids'] == [123.0]


def test_static_private_address_capability_is_never_silently_ignored():
    case = sample('vultr-node')
    case['request']['network']['private_ip'] = '10.42.1.10'
    with pytest.raises(ValueError, match='^unsupported compute static private address$'):
        run(case)


def test_oci_private_ingress_uses_discovered_subnet():
    case = sample('oci-shared')
    case['request']['network'] = {'mode': 'discovered'}
    case['request']['security']['private_filter'] = True
    result = run(case)
    peer = result['inputs']['rules']['peer:private']
    assert peer['cidr'] == 'private'
    assert peer['from_port'] == 9093 and peer['to_port'] == 9094
    assert result['inputs']['rules']['ssh:192.0.2.1/32']['cidr'] == '192.0.2.1/32'
    assert 'existing' in result['documents']['shared.tf.json']['data']['oci_core_subnet']
