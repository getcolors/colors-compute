from copy import deepcopy
import json
import pytest
from colors_compute.power import power_deployment, provider_power
from test_orchestration import Runtime
from test_coordinator import OPTS
ID='12345678-1234-1234-1234-123456789abc'
async def prepared():
    runtime=Runtime();assert (await runtime.run(count=1))['status']=='ready'
    runtime.states['demo/compute/nodes/0.tfstate']['params']['provider_id']=ID
    async def write(opts,intent,env):return await runtime.store.write(intent)
    return runtime,{'journal_get':lambda *_:deepcopy(runtime.store.observed),'journal_put':write,'read_state':lambda opts,key,env:{'status':'present','params':deepcopy(runtime.states[key]['params'])}}
@pytest.mark.asyncio
async def test_power_reads_owned_id_under_lease_and_returns_current_address():
    runtime,deps=await prepared()
    async def power(opts,action,identity,env):
        assert runtime.store.observed['document']['lock']['state']=='held'
        assert identity==ID and action=='start'
        return {'status':'ready','ip':'203.0.113.20'}
    deps['provider_power']=power
    result=await power_deployment(OPTS,'start',{},deps)
    assert result['status']=='ready' and result['cluster']['nodes'][0]['ip']=='203.0.113.20'
    assert runtime.store.observed['document']['lock']['state']=='idle'
    assert runtime.states['demo/compute/nodes/0.tfstate']['params']['ip']=='192.0.2.1'
@pytest.mark.asyncio
async def test_uncertain_provider_result_retains_lock_and_refuses_next_run():
    runtime,deps=await prepared();calls=[]
    deps['provider_power']=lambda *args:calls.append(args) or {'status':'error'}
    assert await power_deployment(OPTS,'stop',{},deps)=={'status':'error'}
    assert runtime.store.observed['document']['lock']['state']=='held'
    assert await power_deployment(OPTS,'stop',{},deps)=={'status':'error'}
    assert len(calls)==1
@pytest.mark.asyncio
async def test_missing_id_never_dispatches_and_releases_and_unsupported_is_offline():
    runtime,deps=await prepared();runtime.states['demo/compute/nodes/0.tfstate']['params'].pop('provider_id')
    deps['provider_power']=lambda *_:pytest.fail('must not dispatch')
    assert await power_deployment(OPTS,'start',{},deps)=={'status':'error'}
    assert runtime.store.observed['document']['lock']['state']=='idle'
    assert await power_deployment({'provider-compute':'aws'},'start',{}, {})=={'status':'error'}
    assert await power_deployment({**OPTS,'blue/event':'build'},'start',{}, {})=={'status':'planned','action':'start'}
@pytest.mark.asyncio
async def test_vultr_single_mutation_poll_and_identity_guard():
    calls=[];states=iter(['stopped','stopped','running'])
    async def http(method,url,headers):
        assert headers['Authorization']=='Bearer synthetic-token';calls.append((method,url))
        return '' if method=='POST' else json.dumps({'instance':{'id':ID,'power_status':next(states),'main_ip':'203.0.113.9'}})
    assert await provider_power(OPTS,'start',ID,{'COLORS_PAR_VULTR_API_KEY':'synthetic-token'},{'http':http,'sleep':lambda _:None})=={'status':'ready','ip':'203.0.113.9'}
    assert [m for m,_ in calls]==['GET','POST','GET','GET'] and calls[1][1].endswith('/'+ID+'/start')
    with pytest.raises(ValueError):await provider_power(OPTS,'stop',ID,{'COLORS_PAR_VULTR_API_KEY':'synthetic-token'},{'http':lambda *_:json.dumps({'instance':{'id':'different','power_status':'running'}})})
@pytest.mark.asyncio
async def test_oci_profile_soft_stop_and_exact_environment():
    identity='ocid1.instance.oc1.example';calls=[]
    async def runner(args,cwd,env,timeout):
        calls.append(args);assert args[:5]==['oci','--config-file','/temporary/.oci/config','--profile','OPERATOR']
        assert 'TF_LOG' not in env and 'OCI_CLI_ENDPOINT' not in env and env['OCI_CLI_AUTH']=='security_token'
        assert '--no-retry' in args
        return {'exit':0,'out':json.dumps({'data':{'id':identity,'lifecycle-state':'RUNNING' if len(calls)==1 else 'STOPPED'}})}
    assert await provider_power({'provider-compute':'oci','oci-config-file-profile':'OPERATOR'},'stop',identity,{'HOME':'/temporary','TF_LOG':'TRACE','OCI_CLI_ENDPOINT':'https://untrusted.invalid','OCI_CLI_AUTH':'security_token'},{'runner':runner})=={'status':'ready'}
    assert len(calls)==3 and 'SOFTSTOP' in calls[1] and '--wait-for-state' in calls[1]
