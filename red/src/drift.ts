import {constants,openSync,closeSync,fstatSync,readSync,lstatSync} from 'node:fs';
import {join} from 'node:path';import {homedir} from 'node:os';import {createHash} from 'node:crypto';
import {copy} from './copy.ts';import {collect,expand,state_keys,compute_credential_errors} from './index.ts';
import {Coordinator} from './coordinator.ts';import {journalGet,journalPut} from './journal.ts';import {lifecycleDocumentValid} from './lifecycle.ts';
import {readState} from './backend.ts';import {checkState} from './execution.ts';import {mode} from './ssh.ts';import {key_request} from './key-request.ts';
import {deployment_requests} from './deployment-request.ts';import {provider_request} from './provider-request.ts';
type Map=Record<string,any>;
export function readDriftPublicKey(opts:Map,fingerprint:string,environment:Map):string{
 const dir=join(environment.HOME||homedir(),'.ssh'),info=lstatSync(dir);if(info.isSymbolicLink()||!info.isDirectory())throw Error('invalid SSH public directory');
 const fd=openSync(join(dir,opts.profile+'.pub'),constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);try{
  if(!fstatSync(fd).isFile())throw Error('invalid SSH public file');const data=Buffer.alloc(65537);let size=0,n=0;while(size<data.length&&(n=readSync(fd,data,size,data.length-size,null))>0)size+=n;
  if(size>65536)throw Error('invalid SSH public file');const value=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(data.subarray(0,size)).trim(),parts=value.split(/\s+/),blob=Buffer.from(parts[1]??'','base64');
  if(parts[0]!=='ssh-ed25519'||value.includes('\n')||value.includes('\r')||blob.toString('base64')!==parts[1]||blob.length!==51||blob.subarray(0,15).toString('hex')!=='0000000b7373682d65643235353139'||blob.readUInt32BE(15)!==32||'SHA256:'+createHash('sha256').update(blob).digest('base64').replace(/=+$/,'')!==fingerprint)throw Error('SSH public fingerprint mismatch');
  return value;
 }finally{closeSync(fd);}
}
export async function check_deployment_drift(input:Map,topologyInput:Map[],requestInput:Map,environment:Map=process.env,dependencies:Map={}):Promise<Map>{
 const opts=copy(input),topology=copy(topologyInput),requirements=copy(requestInput),env={...environment},deps=dependencies;
 let owner:Coordinator|undefined,acquired=false,cancelled:any,declarations:any[]=[];
 const call=async(name:string,defaultFn:any,...args:any[])=>await(deps[name]??defaultFn)(...args);
 const require=(value:any)=>{if(!value)throw Error('compute drift refused');};
 const valid=(doc:Map)=>{require(lifecycleDocumentValid(doc)&&doc.status==='active'&&doc.topology_declared&&doc.shared.phase==='ready'&&doc.key.phase==='prepared');require(Object.values(doc.nodes).every((r:any)=>r.phase!=='destroyed'||!r.desired));const active=Object.fromEntries(Object.entries(doc.nodes).filter(([,n]:any)=>n.phase!=='destroyed'));require(Object.keys(active).length===declarations.length);for(const node of declarations){const r:any=active[node.node_id];require(r&&r.phase==='ready'&&r.desired&&r.role===node.role&&r.index===node.index);}};
 const readExisting=async()=>{const observed=await call('journal_get',journalGet,opts,env);require(observed.status==='present');return observed;};
 const execute=async()=>{
  require(!opts['red/dry-run']&&opts['red/event']!=='build');declarations=expand(topology);if(requirements.private===true)declarations=declarations.map(n=>({...n,private:true}));require(declarations.length&&declarations.length<=1000);
  const observed=await readExisting();valid(observed.document);require(observed.document.lock.state==='idle');
  owner=new Coordinator(opts,{environment:env,eventPrefix:'lifecycle/',read:readExisting,write:(intent)=>call('journal_put',journalPut,opts,intent,env)});await owner.acquire();acquired=true;const doc=(await owner.snapshot())!.document;valid(doc);
  const keys=state_keys(opts.profile,declarations.map(n=>n.node_id)),results:Map[]=[];
  const shared=await call('read_state',readState,opts,keys.shared,env,undefined,true);require(shared.status==='present'&&shared.params?.provider===opts['provider-compute']);
  for(const node of declarations){const state=await call('read_state',readState,opts,keys.nodes[node.node_id],env);require(state.status==='present');results.push(state.params??{});}
  const cluster=collect(declarations,results,requirements.entry_node_id??declarations[0].node_id);
  const errors=await call('compute_credential_errors',compute_credential_errors,opts,env);if(errors.length)return {status:'error',errors};
  const selected=mode(opts);require(selected.mode===doc.key.mode);if(selected.mode==='managed')selected.public_key=await call('public_key',readDriftPublicKey,opts,doc.key.fingerprint,env);
  const key=await call('key_request',key_request,opts,selected,env),assembly=deployment_requests(opts,topology,requirements,key);
  if((assembly.shared as Map).roles)(assembly.shared as Map).peers=Object.fromEntries(cluster.nodes.map(n=>[n.node_id,{role:n.role,vpc_ip:n.vpc_ip}]));
  const plans:[string,Map][]=[[keys.shared,provider_request(opts,'shared',assembly.shared).documents],...assembly.nodes.map(n=>[keys.nodes[n.node_id],provider_request(opts,'node',n,shared.outputs).documents] as [string,Map])];
  for(const [key,documents] of plans){const result=await call('check_state',checkState,opts,key,documents,env);require(result.status==='clean'&&Object.keys(result).length===1);}
  return {status:'clean'};
 };
 let result:Map;try{result=await execute();}catch(error){if(error instanceof Error&&error.name==='AbortError')cancelled=error;result={status:'error'};}
 if(acquired)try{await owner!.release();}catch(error){if(error instanceof Error&&error.name==='AbortError')cancelled=error;result={status:'error'};}
 if(cancelled)throw cancelled;return result;
}
