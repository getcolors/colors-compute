import base64
import json
from pathlib import Path
import stat

import pytest
from colors_compute.managed import managed_kubernetes, plan_managed_kubernetes, managed_errors
from colors_compute.managed_access import AccessDecoder
from colors_compute.coordinator import Coordinator
from test_coordinator import Store

OPTS={'profile':'demo','provider-compute':'vultr','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1','vultr-region':'ams','vultr-vke-version':'v1.35.1','vultr-node-plan':'vc2-2c-4gb','vultr-node-count':2,'compute-prevent-destroy':True}
PARAMS={'provider':'vultr','kind':'managed-kubernetes','name':'demo','cluster_id':'cluster-123','endpoint':'https://192.0.2.1'}
CONFIG='apiVersion: v1\nkind: Config\nclusters: [{name: cluster, cluster: {server: https://192.0.2.1}}]\nusers: [{name: operator, user: {token: private-token}}]\ncontexts: [{name: active, context: {cluster: cluster, user: operator}}]\ncurrent-context: active\n'
def state(config=CONFIG):
 return json.dumps({'version':4,'serial':1,'lineage':'fixture','resources':[{'type':'vultr_kubernetes'}],'outputs':{'params':{'value':PARAMS,'sensitive':False},'kubeconfig_b64':{'value':base64.b64encode(config.encode()).decode(),'sensitive':True}}})

@pytest.mark.parametrize('provider',['vultr','digitalocean'])
def test_plan_owns_only_managed_cluster(tmp_path,provider):
 opts={**OPTS,'workdir':str(tmp_path),'provider-compute':provider,'digitalocean-region':'ams3','doks-version':'1.35.1-do.1','digitalocean-node-size':'s-2vcpu-4gb','digitalocean-node-count':2}
 plan=plan_managed_kubernetes(opts)
 assert plan['status']=='planned'
 assert len(plan['documents'])==2
 assert 'backend.tf.json' in plan['documents']
 assert set(next(iter(plan['documents'].values()))['resource'])=={provider+'_kubernetes' if provider=='vultr' else 'digitalocean_kubernetes_cluster'}
 assert not list(tmp_path.iterdir())

@pytest.mark.parametrize('patch',[{'vultr-node-count':True},{'vultr-node-count':10**400},{'provider-compute':'aws'},{'compute-prevent-destroy':'false'}])
def test_invalid_settings_are_closed(tmp_path,patch):
 assert managed_errors({**OPTS,'workdir':str(tmp_path),**patch})

@pytest.mark.parametrize('payload',['{apiVersion: v1, kind: Config, users: [{user: {exec: {command: sh}}}]}','{"apiVersion":"v1","kind":"Config","users":[{"user":{"tokenFile":"/secret"}}]}','apiVersion: v1\nkind: Config\nx: &a [*a]\n'])
def test_kubeconfig_cannot_execute_or_read_local_files(tmp_path,payload):
 decoder=AccessDecoder({**OPTS,'workdir':str(tmp_path)})
 with pytest.raises(ValueError):decoder(state(payload))
 assert not list(tmp_path.iterdir())

@pytest.mark.asyncio
async def test_journal_create_keeps_kubeconfig_private_and_delete_retires(tmp_path):
 store=Store(); exists=False;calls=[]
 def coordinator(opts,env,**kwargs):return Coordinator(opts,env,store.read,store.write,**kwargs)
 async def presence(*args,**kwargs):return {'status':'present' if exists else 'absent'}
 async def read(*args,**kwargs):return {'status':'present','params':PARAMS}
 async def converge(opts,key,docs,operation,presence,env,decoder):
  nonlocal exists
  assert store.observed['document']['lock']['state']=='held'
  assert store.observed['document']['shared']['phase']=='running'
  calls.append(operation)
  exists=operation=='create'
  if operation=='delete':return {'status':'destroyed'}
  return {'status':'ready','params':PARAMS,'outputs':decoder(state())}
 deps={'version_preflight':lambda *args:True,'coordinator':coordinator,'state_presence':presence,'read_managed_state':read,'converge_state':converge}
 opts={**OPTS,'workdir':str(tmp_path)}
 result=await managed_kubernetes(opts,{}, {'COLORS_PAR_VULTR_API_KEY':'fixture'},deps)
 assert result['status']=='ready'
 assert 'private-token' not in json.dumps(result)
 path=Path(result['kubeconfig_path']);assert path.read_text()==CONFIG
 assert stat.S_IMODE(path.stat().st_mode)==0o600
 assert store.observed['document']['lock']['state']=='idle'
 result=await managed_kubernetes({**opts,'blue/event':'delete','compute-prevent-destroy':False},{},{'COLORS_PAR_VULTR_API_KEY':'fixture'},deps)
 assert result=={'status':'destroyed'}
 assert store.observed['document']['status']=='retired'
 assert calls==['create','delete']

@pytest.mark.asyncio
async def test_legacy_state_refused_before_compute_credentials_or_local_mutation(tmp_path):
 store=Store();calls=[]
 def coordinator(opts,env,**kwargs):return Coordinator(opts,env,store.read,store.write,**kwargs)
 async def presence(*args,**kwargs):calls.append(args[1]);return {'status':'present'}
 result=await managed_kubernetes({**OPTS,'workdir':str(tmp_path)},{'legacy_state_keys':['demo/legacy.tfstate']},{},{'coordinator':coordinator,'state_presence':presence})
 assert result=={'status':'error'}
 assert calls==['demo/legacy.tfstate']
 assert not list(tmp_path.iterdir())

@pytest.mark.parametrize('before,after', [
 ('current-context: active', 'current-context: missing'),
 ('user: operator', 'user: missing'),
 ('token: private-token', ''),
 ('server: https://192.0.2.1', 'server: https://192.0.2.1, proxy-url: http://localhost:1234'),
 ('server: https://192.0.2.1', 'server: https://192.0.2.1, tls-server-name: other.example'),
])
def test_active_context_and_transport_are_bound(tmp_path, before, after):
 decoder = AccessDecoder({**OPTS, 'workdir': str(tmp_path)})
 with pytest.raises(ValueError):
  decoder(state(CONFIG.replace(before, after)))
 assert not list(tmp_path.iterdir())
