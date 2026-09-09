import json
from pathlib import Path
import pytest
from colors_compute.planning import plan_deployment

CASES = json.loads((Path(__file__).parents[2] / 'test/fixtures/provider-requests.json').read_text())


@pytest.mark.parametrize('provider', ['aws', 'azure', 'digitalocean', 'google', 'hcloud', 'oci', 'vultr', 'yandex'])
def test_plan_without_credentials_or_local_ssh(provider, monkeypatch):
    fixture = next(case['args'] for case in CASES if case['args'][0]['provider-compute'] == provider and case['args'][1] == 'shared')
    opts, _, request = fixture[:3]
    monkeypatch.setenv('HOME', '/does-not-exist')
    result = plan_deployment(opts, [{'count': 3}], {'network': request['network'], 'security': request['security']})
    assert result['status'] == 'planned'
    assert [node['ip'] for node in result['cluster']['nodes']] == ['192.0.2.10', '192.0.2.11', '192.0.2.12']
    assert list(result['documents']['nodes']) == ['0', '1', '2']
    assert all(node['provider'] == provider for node in result['cluster']['nodes'])
    assert result == plan_deployment(opts, [{'count': 3}], {'network': request['network'], 'security': request['security']})
