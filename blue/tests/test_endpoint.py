import importlib.util
import json
from pathlib import Path
import pytest
from colors_compute.provider_request import provider_request
from colors_compute.endpoint import endpoint_agent
ROOT=Path(__file__).parents[2]
CASES=json.loads((ROOT/'test/fixtures/provider-endpoint.json').read_text())
@pytest.mark.parametrize('case',CASES,ids=lambda case:case['name'])
def test_request(case):
    if 'error' in case['expected']:
        with pytest.raises(ValueError,match=case['expected']['error']):provider_request(*case['args'])
    else:assert provider_request(*case['args'])==case['expected']

spec=importlib.util.spec_from_file_location('endpoint_agent',ROOT/'agents/endpoint-agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
ENV={'COLORS_PAR_DO_TOKEN':'synthetic-token'}
IP='198.51.100.10'
class Store:
    def __init__(self,holder=1):self.holder=holder;self.calls=[];self.fail=False;self.pending=None
    def request(self,method,path,token,body,timeout):
        self.calls.append((method,path,body));assert token=='synthetic-token';assert 0<timeout<=20
        if self.fail:raise RuntimeError('synthetic-token')
        if method=='GET':return {'reserved_ip':{'ip':IP,'droplet':None if self.holder is None else {'id':self.holder}}}
        assert path.endswith('/actions')
        self.holder=None if body['type']=='unassign' else body['droplet_id']
        return {'action':{'status':'in-progress'}}

def test_moves_existing_endpoint_and_confirms_without_creating_resources():
    store=Store();result=agent.operate('digitalocean',IP,'2',environment=ENV,transport=store.request,sleep=lambda _:None)
    assert result=={'status':'assigned','node_id':'2'}
    assert [call[2] for call in store.calls if call[0]=='POST']==[{'type':'unassign'},{'type':'assign','droplet_id':2}]
    assert all(call[1].startswith('/v2/reserved_ips/'+IP) for call in store.calls)
    store.calls=[];assert agent.operate('digitalocean',IP,'2',environment=ENV,transport=store.request)['status']=='assigned'
    assert len(store.calls)==1

def test_read_errors_and_malformed_observations_prevent_every_mutation():
    store=Store();store.fail=True
    with pytest.raises(agent.EndpointError,match='API request failed'):agent.operate('digitalocean',IP,'2',environment=ENV,transport=store.request)
    assert [call[0] for call in store.calls]==['GET']
    for body in [{},{'reserved_ip':{'ip':IP}},{'reserved_ip':{'ip':'203.0.113.1','droplet':None}},{'reserved_ip':{'ip':IP,'droplet':{'id':True}}}]:
        calls=[]
        def request(method,*args):calls.append(method);return body
        with pytest.raises(agent.EndpointError):agent.operate('digitalocean',IP,'2',environment=ENV,transport=request)
        assert calls==['GET']

def test_status_does_not_mutate_or_require_target():
    store=Store(None);assert agent.operate('digitalocean',IP,action='status',environment=ENV,transport=store.request)=={'status':'present','node_id':None}
    assert len(store.calls)==1

@pytest.mark.parametrize('provider,ip,node',[('aws',IP,'1'),('digitalocean','../other','1'),('digitalocean',IP,'1;bad'),('digitalocean',IP,'0')])
def test_invalid_request_has_no_network(provider,ip,node):
    calls=[]
    with pytest.raises(agent.EndpointError):agent.operate(provider,ip,node,environment=ENV,transport=lambda *args:calls.append(args))
    assert calls==[]

def test_timeout_and_unconfirmed_action_never_claim_success():
    calls=[]
    def transport(method,path,token,body,timeout):
        calls.append((method,body))
        return {'reserved_ip':{'ip':IP,'droplet':{'id':1}}} if method=='GET' else {'action':{'status':'in-progress'}}
    with pytest.raises(agent.EndpointError,match='timed out'):agent.operate('digitalocean',IP,'2',environment=ENV,transport=transport,sleep=lambda _:None)
    assert [body for method,body in calls if method=='POST']==[{'type':'unassign'}]

def test_artifact_resource_and_descriptor():
    artifact=endpoint_agent('digitalocean');assert artifact['content']==(ROOT/'agents/endpoint-agent.py').read_text()
    assert artifact['credentials']==['COLORS_PAR_DO_TOKEN']
    with pytest.raises(ValueError):endpoint_agent('aws')

def test_standalone_cli_reports_only_generic_failure(tmp_path):
    import subprocess, sys
    path=tmp_path/'agent.py';path.write_text(endpoint_agent('digitalocean')['content'])
    result=subprocess.run([sys.executable,str(path),'--provider','digitalocean','--ip',IP,'--node-id','2'],env={},capture_output=True,text=True)
    assert result.returncode==1 and result.stdout=='{"status":"error"}\n' and result.stderr==''
