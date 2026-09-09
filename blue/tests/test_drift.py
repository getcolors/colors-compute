from copy import deepcopy
import json
from pathlib import Path
import pytest
from colors_compute.drift import check_deployment_drift, _public_key
from colors_compute.execution import check_state
from colors_compute.backend import ProcessResult
from test_orchestration import Runtime
from test_execution import Runner, OPTS as EXEC_OPTS, ENV, KEY, DOCUMENTS, STATE, EMPTY

CASES = json.loads((Path(__file__).parents[2] / 'test/fixtures/provider-requests.json').read_text())
OPTS, _, BASE = next(c['args'][:3] for c in CASES if c['args'][0]['provider-compute']=='vultr' and c['args'][1]=='shared')
REQ = {'security': BASE['security'], 'network': BASE['network']}

@pytest.mark.asyncio
async def test_all_states_are_planned_under_existing_journal_without_lifecycle_transitions():
    runtime = Runtime()
    assert (await runtime.run())['status'] == 'ready'
    opts = {**OPTS, 'profile':'demo','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'}
    runtime.states['demo/compute/shared.tfstate'].update(ssh_key_id='test-key')
    runtime.states['demo/compute/shared.tfstate']['params'].update(vpc_id='test-vpc',firewall_group_id='test-firewall')
    plans=[]
    async def write(opts,intent,env):
        if intent['condition']['if_match'] != runtime.store.observed['etag']: return {'status':'conflict'}
        runtime.store.observed={'status':'present','etag':'drift-'+intent['document']['write_id'],'document':deepcopy(intent['document'])}
        return {'status':'written','etag':runtime.store.observed['etag']}
    async def read(opts,key,env,include_outputs=False):
        return {'status':'present','params':runtime.states[key]['params'],'outputs':runtime.states[key]}
    async def check(opts,key,docs,env):
        assert runtime.store.observed['document']['lock']['state']=='held'
        plans.append(key)
        return {'status':'clean'}
    deps={'journal_get':lambda *_:deepcopy(runtime.store.observed),'journal_put':write,'read_state':read,
          'public_key':lambda *_:'ssh-ed25519 fixture','compute_credential_errors':lambda *_:[], 'check_state':check}
    before=deepcopy(runtime.store.observed['document'])
    assert await check_deployment_drift(opts,[{'count':2}],REQ,{},deps)=={'status':'clean'}
    assert plans==['demo/compute/shared.tfstate','demo/compute/nodes/0.tfstate','demo/compute/nodes/1.tfstate']
    after=runtime.store.observed['document']
    for key in ['nodes','shared','key','generation','status','topology_declared']: assert before[key]==after[key]
    assert after['lock']['state']=='idle'
    assert await check_deployment_drift(opts,[{'count':2}],{**REQ,'private':True},{},deps)=={'status':'error'}
    assert len(plans)==3
    deps['check_state']=lambda *_:{'status':'error'}
    assert await check_deployment_drift(opts,[{'count':2}],REQ,{},deps)=={'status':'error'}
    assert runtime.store.observed['document']['lock']['state']=='idle'
    plans.clear()
    assert await check_deployment_drift(opts,[{'count':1}],REQ,{},deps)=={'status':'error'}
    assert plans==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('exit',[0,1,2,-1])
async def test_drift_executor_accepts_only_zero_plan_and_never_applies(exit):
    base=Runner(before=json.dumps(STATE))
    async def runner(args,cwd,env,timeout):
        result=await base(args,cwd,env,timeout)
        return ProcessResult(exit,'','secret diagnostic') if args[1]=='plan' else result
    assert await check_state(EXEC_OPTS,KEY,DOCUMENTS,ENV,runner)=={'status':'clean' if exit==0 else 'error'}
    assert [c[1] for c in base.calls]==['init','state','plan']
    assert '-detailed-exitcode' in base.calls[-1]
    assert all(not p.exists() for p in base.paths)

@pytest.mark.asyncio
async def test_absent_or_empty_state_cannot_pass_drift():
    for before in ['',json.dumps(EMPTY)]:
        runner=Runner(before=before)
        assert await check_state(EXEC_OPTS,KEY,DOCUMENTS,ENV,runner)=={'status':'error'}
        assert all(c[1]!='plan' for c in runner.calls)

def test_owned_public_key_reads_no_private_file_and_preserves_permissions(tmp_path):
    import base64, hashlib
    directory=tmp_path/'.ssh';directory.mkdir(mode=0o755)
    blob=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'+bytes(32)
    public='ssh-ed25519 '+base64.b64encode(blob).decode()+' comment'
    fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    path=directory/'demo.pub';path.write_text(public);path.chmod(0o644)
    assert _public_key({'profile':'demo'},fingerprint,{'HOME':str(tmp_path)})==public
    assert not (directory/'demo').exists() and path.stat().st_mode&0o777==0o644 and directory.stat().st_mode&0o777==0o755
    with pytest.raises(ValueError): _public_key({'profile':'demo'},'SHA256:'+'A'*43,{'HOME':str(tmp_path)})
    path.unlink();path.symlink_to(tmp_path/'target')
    with pytest.raises(OSError):_public_key({'profile':'demo'},fingerprint,{'HOME':str(tmp_path)})


def test_public_fifo_is_refused_without_waiting_for_a_writer(tmp_path):
    import os
    directory=tmp_path/'.ssh';directory.mkdir();os.mkfifo(directory/'demo.pub')
    with pytest.raises(ValueError):_public_key({'profile':'demo'},'SHA256:'+'A'*43,{'HOME':str(tmp_path)})
