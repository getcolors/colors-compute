"""Live R2 probe: create one disposable SSH resource, then delete to a tombstone.

Requires COLORS_PAR_R2_ACCESS_KEY_ID and COLORS_PAR_R2_SECRET_ACCESS_KEY.
All writes are profile-namespaced; no compute or provider registration is created.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'blue/src'))
from colors_compute.ssh import ssh_resource,start_agent
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--bucket',required=True)
p.add_argument('--endpoint',required=True)
p.add_argument('--profile',required=True)
a=p.parse_args()
async def main():
 with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as other:
  opts={'profile':a.profile,'provider-backend':'r2','r2-bucket':a.bucket,'r2-endpoint':a.endpoint}
  req={'name':'protocol-probe-'+uuid.uuid4().hex[:12],'workdir':str(Path(first).resolve()),'passphrase_env':'COLORS_PAR_PROBE_OLD'}
  env=dict(os.environ,COLORS_PAR_PROBE_OLD=secrets.token_urlsafe(48),COLORS_PAR_PROBE_NEW=secrets.token_urlsafe(48))
  results=await asyncio.gather(*(ssh_resource(opts,req,environment=env) for _ in range(2)))
  ready=[r for r in results if r.get('status')=='ready']
  assert ready,'no concurrent creator succeeded'
  resource=ready[0]
  assert all(r==resource for r in ready),'concurrent creators produced different identities'
  assert await ssh_resource(opts,req,environment=env)==resource,'retry changed identity'
  rotated=await ssh_resource(opts,dict(req,new_passphrase_env='COLORS_PAR_PROBE_NEW',expected=resource),'rotate',env)
  assert rotated==resource,'rotation failed or changed public identity'
  recovered_req=dict(req,workdir=str(Path(other).resolve()),passphrase_env='COLORS_PAR_PROBE_NEW',expected=resource)
  assert await ssh_resource(opts,recovered_req,'inspect',env)==resource,'fresh-workstation retrieval failed'
  cleanups=[]
  try:
   agent=await start_agent([{'opts':opts,'request':recovered_req,'resource':resource}],env,lambda phase,close:cleanups.append(close))
   assert agent['status']=='ready'
  finally:
   for close in cleanups:await close()
  deleted=await ssh_resource(opts,dict(recovered_req,allow_delete=True,consumers_destroyed=True),'delete',env)
  assert deleted['status']=='destroyed','deletion failed'
  refused=await ssh_resource(opts,req,'create',env)
  assert refused.get('status')=='error','tombstone allowed regeneration'
  print(json.dumps({'profile':a.profile,'resource':req['name'],'concurrent_creators':len(ready),'single_identity':True,'rotation_preserved_identity':True,'fresh_workstation_access':True,'agent_cleanup':not Path(agent['socket']).exists(),'deleted_to_tombstone':True}))
asyncio.run(main())
