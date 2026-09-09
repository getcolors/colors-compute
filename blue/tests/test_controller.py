import pytest
from colors_compute.controller import controller_artifact
from colors_compute.deployment_request import deployment_requests

OPTS={'profile':'demo','provider-compute':'digitalocean','digitalocean-cloud-controller-version':'v0.1.68'}
SHARED={'params':{'provider':'digitalocean','vpc_id':'network-123'}}


def test_controller_artifact_contains_only_literal_credential_lookup_and_owned_network():
    result=controller_artifact({**OPTS,'do-token':'must-not-render'},SHARED)
    assert result['filename']=='colors-compute-controller.yml'
    assert result['credentials']==['COLORS_PAR_DO_TOKEN']
    assert "lookup('ansible.builtin.env', 'COLORS_PAR_DO_TOKEN')" in result['content']
    assert 'no_log: true' in result['content']
    assert 'must-not-render' not in result['content']
    assert 'DO_CLUSTER_VPC_ID=network-123' in result['content']
    assert result['rollout_resource']=='deployment/digitalocean-cloud-controller-manager'
    result['credentials'].append('changed')
    assert controller_artifact(OPTS,SHARED)['credentials']==['COLORS_PAR_DO_TOKEN']


@pytest.mark.parametrize('opts,shared',[
    ({**OPTS,'provider-compute':'vultr'},SHARED),
    ({**OPTS,'digitalocean-cloud-controller-version':'v1.2.3; id'},SHARED),
    ({**OPTS,'kubernetes-controller-version':False},SHARED),
    (OPTS,{'params':{'provider':'digitalocean','vpc_id':'abc\nDO_TOKEN=oops'}}),
    (OPTS,{'params':{'provider':'aws','vpc_id':'network-123'}}),
])
def test_controller_invalid_capability_or_bindings_fail_closed(opts,shared):
    with pytest.raises(ValueError):controller_artifact(opts,shared)


def test_controller_requirement_validates_before_node_assembly():
    req={'kubernetes_controller':True,'security':{}}
    deployment_requests(OPTS,[{'count':1}],req,{'mode':'managed'})
    with pytest.raises(ValueError,match='controller requirement'):
        deployment_requests(OPTS,[{'count':1}],{**req,'kubernetes_controller':False},{'mode':'managed'})
