import {join} from 'node:path';
import {homedir} from 'node:os';
import {readState} from './backend.ts';
import {collect,state_keys} from './index.ts';
import {identityEqual} from './coordination.ts';
import {journalGet,identity} from './journal.ts';
import {lifecycleDocumentValid} from './lifecycle.ts';
import {mode} from './ssh.ts';
import {backend_presence} from './managed-backend.ts';
type Map=Record<string,any>;
export async function read_deployment(opts:Map,environment:Map=process.env,dependencies:Map={},requirements:Map={}) {
 const env={...environment},call=async(name:string,fn:any,...args:any[])=>await (dependencies[name]??fn)(...args);
 try{
  if(requirements===null||typeof requirements!=='object'||Array.isArray(requirements))return {status:'error'};
  let observed=await call('journal_get',journalGet,opts,env);
  // A managed bucket that no longer exists is a retired deployment, not a transport error.
  if(observed.status!=='present'&&!(observed.status==='absent'&&Object.keys(observed).length===1)){
   const presence=await call('backend_presence',backend_presence,opts,env);
   if(presence?.status==='absent'&&Object.keys(presence).length===1)observed={status:'absent'};
  }
  if(observed.status==='absent'&&Object.keys(observed).length===1)return {status:'absent'};
  if(observed.status!=='present')return {status:'error'};
  const doc=observed.document;
  if(!lifecycleDocumentValid(doc)||!identityEqual(doc.identity,identity(opts)))return {status:'error'};
  if(doc.status==='retired')return {status:'destroyed'};
  if(doc.lock.state!=='idle')return {status:'error'};
  const shared=await call('read_state',readState,opts,state_keys(opts.profile,[]).shared,env,undefined,true);
  if(shared.status!=='present'||shared.params?.provider!==opts['provider-compute']||!shared.outputs)return {status:'error'};
  const records=Object.values(doc.nodes).filter((node:any)=>node.phase!=='destroyed') as Map[];
  if(records.length&&records.every(node=>node.phase==='declared')){
   if(doc.shared.phase!=='ready'||doc.key.phase!=='prepared'||mode(opts).mode!==doc.key.mode)return {status:'error'};
   for(const node of records){const state=await call('read_state',readState,opts,node.state_key,env,undefined,true);if(!(state.status==='absent'&&Object.keys(state).length===1)&&!(state.status==='present'&&state.state_empty===true))return {status:'error'};}
   return {status:'partial'};
  }
  const declarations:Map[]=[],results:Map[]=[];
  for(const [id,node] of Object.entries(doc.nodes) as [string,Map][]){
   if(node.phase==='destroyed')continue;if(!['ready','failed'].includes(node.phase))return {status:'error'};
   const state=await call('read_state',readState,opts,node.state_key,env);if(state.status!=='present')return {status:'error'};
   declarations.push({node_id:id,role:node.role,index:node.index,provider:opts['provider-compute']});results.push(state.params??{});
  }
  if(!declarations.length)return {status:'error'};
  declarations.sort((a,b)=>(a.role??'')<(b.role??'')?-1:(a.role??'')>(b.role??'')?1:a.index-b.index);
  const key:Map={mode:doc.key.mode},selected=mode(opts);if(selected.mode!==key.mode)return {status:'error'};
  if(key.mode==='managed')key.private_key_path=join(env.HOME||homedir(),'.ssh',opts.profile);
  else if(selected.private_key_path)key.private_key_path=selected.private_key_path;
  const entry=Object.hasOwn(requirements,'entry_node_id')?requirements.entry_node_id:declarations[0].node_id;
  if(typeof entry!=='string'||!declarations.some(node=>node.node_id===entry))return {status:'error'};
  const cluster=collect(declarations,results,entry);
  if(key.private_key_path)for(const node of cluster.nodes)node.ssh_identity_file=key.private_key_path;
  return {status:'present',cluster,shared:shared.outputs,key};
 }catch(error){if(error instanceof Error&&error.name==='AbortError')throw error;return {status:'error'};}
}
