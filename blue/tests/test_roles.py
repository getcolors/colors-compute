from copy import deepcopy
import json
from pathlib import Path
import pytest
from colors_compute.deployment_request import deployment_requests
from colors_compute.planning import plan_deployment
from colors_compute.provider_request import provider_request

CASES = json.loads((Path(__file__).parents[2] / 'test/fixtures/provider-requests.json').read_text())
OPTS, _, BASE = next(c['args'][:3] for c in CASES if c['args'][0]['provider-compute'] == 'vultr' and c['args'][1] == 'shared')
TOPOLOGY = [{'role': 'db', 'count': 2}, {'role': 'app', 'count': 1}]
SSH = {'id': 'ssh', 'protocol': 'tcp', 'from_port': 22, 'to_port': 22, 'sources': ['192.0.2.0/24']}
PEER = {'id': 'db', 'protocol': 'tcp', 'from_port': 5432, 'to_port': 5432, 'peer_roles': ['app']}
POLICY = {'ingress': [SSH], 'egress': 'all', 'private_filter': True}
REQ = {'security': POLICY, 'network': BASE['network'], 'entry_node_id': 'app-0',
       'roles': {'db': {'security': {**POLICY, 'ingress': [SSH, PEER]}}, 'app': {'security': POLICY}}}


def test_role_plans_entry_and_exact_stable_peer_firewalls():
    opts = {**OPTS, 'vultr-plan-db': 'vc2-4c-8gb', 'compute-role-settings': {'app': {'size': 'vc2-2c-4gb'}}}
    result = plan_deployment(opts, TOPOLOGY, REQ)
    assert result['cluster']['entry_node_id'] == 'app-0'
    assert result['documents']['nodes']['db-0']['node.tf.json']['resource']['vultr_instance']['node']['plan'] == 'vc2-4c-8gb'
    assert result['documents']['nodes']['app-0']['node.tf.json']['resource']['vultr_instance']['node']['plan'] == 'vc2-2c-4gb'
    shared = result['documents']['shared']['shared-roles.tf.json']
    rules = shared['locals']['ingress']
    assert rules['db:db:peer:app-0']['subnet_size'] == 32
    assert rules['db:db:peer:app-0']['subnet'] == result['cluster']['nodes'][-1]['vpc_ip']
    assert rules['db:db:peer:app-0']['role'] == 'db'
    assert not any(key.startswith('app:db:') for key in rules)
    assert set(shared['resource']['vultr_firewall_group']['role']['for_each']) == {'app', 'db'}
    assert 'role_firewall_ids' in shared['output']['params']['value']


def test_first_plan_has_no_speculative_peer_rules_then_observed_ip_changes_keep_address():
    request = deployment_requests(OPTS, TOPOLOGY, REQ, BASE['key'])['shared']
    initial = provider_request(OPTS, 'shared', request)
    assert not any(':peer:' in key for key in initial['inputs']['role_ingress'])
    request['peers'] = {'app-0': {'role': 'app', 'vpc_ip': '10.42.1.5'}}
    before = provider_request(OPTS, 'shared', request)
    request['peers']['app-0']['vpc_ip'] = '10.42.1.6'
    after = provider_request(OPTS, 'shared', request)
    assert before['inputs']['role_ingress'].keys() == after['inputs']['role_ingress'].keys()
    assert before['inputs']['role_ingress']['db:db:peer:app-0']['subnet'] == '10.42.1.5'


@pytest.mark.parametrize('change', [
    {'entry_node_id': 'absent'}, {'roles': {}},
    {'roles': {'db': {'security': {**POLICY, 'ingress': [SSH, {**PEER, 'peer_roles': ['unknown']}] }}, 'app': {'security': POLICY}}},
])
def test_invalid_topology_role_policy_rejected(change):
    with pytest.raises(ValueError):
        plan_deployment(OPTS, TOPOLOGY, {**REQ, **change})


def test_ipv6_public_ingress_and_explicit_capability_refusal():
    req = deepcopy(REQ)
    req['roles']['app']['security']['ingress'].append({**SSH, 'id': 'ssh-v6', 'sources': ['2001:db8::/32']})
    result = plan_deployment(OPTS, TOPOLOGY, req)
    assert result['documents']['shared']['shared-roles.tf.json']['locals']['ingress']['app:ssh-v6:2001:db8::/32']['ip_type'] == 'v6'
    with pytest.raises(ValueError, match='role firewall'):
        plan_deployment({**OPTS, 'provider-compute': 'aws'}, TOPOLOGY, REQ)

@pytest.mark.asyncio
async def test_native_fanout_then_peer_gate_preserves_observed_peers_on_reconverge():
    from test_orchestration import Runtime
    from test_coordinator import OPTS as BACKEND
    from colors_compute.orchestration import orchestrate
    class RolesRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self.shared_plans = []
        async def read(self, opts, key, env, include_outputs=False):
            return {'status': 'present', 'params': self.states[key]['params'], 'outputs': self.states[key]}
        async def converge(self, opts, key, docs, operation, presence, env):
            if key.endswith('/shared.tfstate'):
                self.events.append('shared')
                self.shared_plans.append(deepcopy(docs['shared-roles.tf.json']['locals']['ingress']))
                outputs = {'ssh_key_id': 'test-key', 'params': {'provider': 'vultr', 'vpc_id': 'test-vpc',
                    'role_firewall_ids': {'db': 'db-fw', 'app': 'app-fw'}}}
            else:
                node = docs['node.tf.json']['resource']['vultr_instance']['node']
                node_id = key.rsplit('/', 1)[-1].removesuffix('.tfstate')
                self.events.append(node_id)
                outputs = {'params': {'node_id': node_id, 'provider': 'vultr', 'name': node['label'],
                    'ip': '192.0.2.10', 'vpc_ip': '10.42.1.' + str({'db-0': 10, 'db-1': 11, 'app-0': 12}[node_id]), 'user': 'root', 'sudoer': 'root'}}
            self.states[key] = outputs
            return {'status': 'ready', 'params': outputs['params'], 'outputs': outputs}
    runtime = RolesRuntime()
    deps = runtime.dependencies()
    deps['deployment_requests'] = deployment_requests
    deps['provider_request'] = provider_request
    opts = {**BACKEND, **OPTS, 'profile': 'demo', 'blue/event': 'create'}
    first = await orchestrate(opts, TOPOLOGY, REQ, {}, deps)
    assert first['status'] == 'ready'
    assert first['cluster']['entry_node_id'] == 'app-0'
    assert runtime.events[0] == runtime.events[-1] == 'shared'
    assert not any(':peer:' in key for key in runtime.shared_plans[0])
    assert runtime.shared_plans[1]['db:db:peer:app-0']['subnet'] == '10.42.1.12'
    second = await orchestrate(opts, TOPOLOGY, REQ, {}, deps)
    assert second['status'] == 'ready'
    assert runtime.shared_plans[2]['db:db:peer:app-0']['subnet'] == '10.42.1.12'
    assert runtime.store.observed['document']['lock']['state'] == 'idle'

@pytest.mark.parametrize('peers', [
    {'app-0': {'role': [], 'vpc_ip': '10.42.1.12'}},
    {'other-0': {'role': 'app', 'vpc_ip': '10.42.1.12'}},
    {'app-01': {'role': 'app', 'vpc_ip': '10.42.1.12'}},
    {'app-0': {'role': 'app', 'vpc_ip': '203.0.113.5'}},
    {'app-0': {'role': 'app', 'vpc_ip': '10.42.1.12', 'secret': 'ignored?'}},
])
def test_untrusted_peer_identity_and_extra_fields_refused(peers):
    request = deployment_requests(OPTS, TOPOLOGY, REQ, BASE['key'])['shared']
    with pytest.raises(ValueError):
        provider_request(OPTS, 'shared', {**request, 'peers': peers})


@pytest.mark.parametrize('roles', [{'db': {'security': 'bad'}, 'app': {'security': POLICY}},
                                  {'db': {'security': {**POLICY, 'ingress': [None]}}, 'app': {'security': POLICY}}])
def test_malformed_nested_role_policies_fail_closed(roles):
    with pytest.raises(ValueError):
        deployment_requests(OPTS, TOPOLOGY, {**REQ, 'roles': roles}, BASE['key'])
