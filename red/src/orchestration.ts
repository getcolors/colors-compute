import {copy} from './copy.ts';
import {validate_deployment} from './planning.ts';
import {run} from 'red/workflow';
import {readState} from './backend.ts';
import {expand,state_keys,compute_credential_errors} from './index.ts';
import {Coordinator} from './coordinator.ts';
import {statePresence,convergeState} from './execution.ts';
import {provider_request} from './provider-request.ts';
import {key_request} from './key-request.ts';
import {deployment_requests} from './deployment-request.ts';
import {prepareKeypair,cleanupKeypair,mode} from './ssh.ts';
import {clusterWorkflow} from './workflow.ts';
type Map=Record<string,any>;
export async function orchestrate(input:Map,topologyInput:Map[],requestInput:Map,environment:Map=process.env,dependencies:Map={}) {
 const opts=copy(input),topology=structuredClone(topologyInput),request=structuredClone(requestInput),env={...environment},deps=dependencies;
 let coordinator:any,acquired=false,keys:any;
 const call=async(name:string,defaultFn:any,...args:any[])=>await (deps[name]??defaultFn)(...args);
 const require=(value:any)=>{if(!value)throw Error('compute lifecycle refused');};
 const snapshot=async()=> (await coordinator.snapshot()).document;
 const readable=async(key:string)=>{const r=await call('read_state',readState,opts,key,env,undefined,true);require(r.status==='present'&&r.params?.provider===opts['provider-compute']);return r;};
 const presenceFor=async(key:string,record:Map)=>{
  const p=await call('state_presence',statePresence,opts,key,env);require(p&&Object.keys(p).length===1&&['present','absent'].includes(p.status));
  if(['declared','destroyed'].includes(record.phase)){if(p.status==='present'){const state=await call('read_state',readState,opts,key,env,undefined,true);require(state.status==='present'&&state.state_empty===true);}}else require(p.status==='present');
  if(record.phase==='failed')await readable(key);return p;
 };
 const attempt=async(nodeId:string|null,documents:Map,operation:string)=>{
  const doc=await snapshot(),record=nodeId===null?doc.shared:doc.nodes[nodeId];
  const key=nodeId===null?keys.shared:state_keys(opts.profile,[nodeId]).nodes[nodeId];
  if(operation==='delete'&&record.phase==='destroyed')return {status:'destroyed'};
  const presence=await presenceFor(key,record);
  if(operation==='create'&&record.phase==='failed'){
   require(record.operation==='create');await coordinator.transition(nodeId===null?'shared-retry':'retry',{evidence:'readable-state',...(nodeId===null?{}:{node_id:nodeId})});
  }
  const id=nodeId===null?await (operation==='create'?coordinator.sharedStart():coordinator.sharedDestroy()):await (operation==='create'?coordinator.start(nodeId):coordinator.destroy(nodeId));
  let result:any;
  try{result=operation==='delete'&&record.phase==='declared'&&presence.status==='absent'?{status:'destroyed'}:await call('converge_state',convergeState,opts,key,documents,operation,presence,env);}
  catch(e){if(nodeId===null)await coordinator.sharedFail(id);else await coordinator.fail(nodeId,id);throw e;}
  const success=result.status===(operation==='create'?'ready':'destroyed');
  if(nodeId===null)await (success?coordinator.sharedComplete(id):coordinator.sharedFail(id));else await (success?coordinator.complete(nodeId,id):coordinator.fail(nodeId,id));
  require(success);return result;
 };
 const execute=async()=>{
  let declarations=expand(topology);require(declarations.length&&declarations.length<=1000&&['create','delete'].includes(opts['red/event']??'create')&&opts['red/dry-run']!==true);
  if(request.private===true)declarations=declarations.map(node=>({...node,private:true}));
  const legacy=request.legacy_state_keys??[];require(Array.isArray(legacy)&&new Set(legacy).size===legacy.length);
  for(const key of legacy){const observed=await call('state_presence',statePresence,opts,key,env,undefined,true);require(observed.status==='absent'&&Object.keys(observed).length===1);}
  const operation=opts['red/event']??'create';require(operation!=='delete'||opts['compute-prevent-destroy']===false);
  keys=state_keys(opts.profile,declarations.map(node=>node.node_id));
  coordinator=deps.coordinator?deps.coordinator(opts,{environment:env,eventPrefix:'lifecycle/'}):new Coordinator(opts,{environment:env,eventPrefix:'lifecycle/'});
  require(!Object.hasOwn(opts,'compute-require-existing-state')||typeof opts['compute-require-existing-state']==='boolean');
  await coordinator.acquire(operation==='create'&&(opts['compute-require-existing-state']??false));acquired=true;let doc=await snapshot();
  if(operation==='delete'&&doc.status==='retired')return {status:'destroyed'};
  if(operation==='create'&&doc.status==='retired'){await coordinator.transition('recreate');doc=await snapshot();}
  require(operation==='create'?doc.status==='active':['active','deleting'].includes(doc.status));
  const selected=mode(opts);require(doc.key.mode===null||doc.key.mode===selected.mode);let sharedRead:any=null;const observedNodes:Map={};
  for(const [nodeId,record] of [[null,doc.shared],...Object.entries(doc.nodes)] as [string|null,Map][]){
   const key=nodeId===null?keys.shared:state_keys(opts.profile,[nodeId]).nodes[nodeId];const p=await presenceFor(key,record);
   if(p.status==='present'&&!['declared','destroyed'].includes(record.phase)){const r=await readable(key);if(nodeId===null)sharedRead=r;else observedNodes[nodeId]={role:record.role,vpc_ip:r.params.vpc_ip};}
  }
  if(operation==='create'){
   for(const node of declarations)if(!Object.hasOwn(doc.nodes,node.node_id))await presenceFor(keys.nodes[node.node_id],{phase:'declared'});
   await coordinator.declare(topology);
  }else await coordinator.transition('begin-delete');
  doc=await snapshot();
  const missingCredentials=await call('compute_credential_errors',compute_credential_errors,opts,env);
  if(missingCredentials.length)return {status:'error',errors:missingCredentials};
  if(operation==='create'){
   await call('validate_deployment',validate_deployment,opts,topology,request);
   const defaultPreflight=async(...args:any[])=>{const {registrationPreflight}=await import('./registration.ts');return (registrationPreflight as any)(...args);};
   await call('registration_preflight',defaultPreflight,opts,selected.mode,sharedRead?.outputs?.registration,undefined,env);
  }
  const keyRecord=doc.key;
  if(operation==='delete'&&keyRecord.phase==='absent')require(doc.shared.phase==='declared'&&Object.values(doc.nodes).every((n:any)=>n.phase==='declared'));
  require(['absent','prepared'].includes(keyRecord.phase));
  const ownership=keyRecord.mode==='managed'?{status:'prepared',fingerprint:keyRecord.fingerprint}:{status:'fresh'};
  const recordIntent=async()=>{await coordinator.transition('key-intent',{mode:'managed'});return true;};
  const recordPrepared=async(fingerprint:string)=>{await coordinator.transition('key-prepared',{fingerprint});return true;};
  let key:any;
  if(operation==='create'){
   key=await call('prepare_keypair',prepareKeypair,opts,ownership,env,recordIntent,recordPrepared);
   if(key.mode==='external'&&keyRecord.phase==='absent'){await coordinator.transition('key-intent',{mode:'external'});await coordinator.transition('key-prepared',{fingerprint:null});}
  }else{require(keyRecord.phase==='prepared');key=await call('prepare_keypair',prepareKeypair,{...opts,'red/event':'create'},ownership,env,recordIntent,recordPrepared);}
  const normalized=await call('key_request',key_request,opts,key,env);
  const assembly=await call('deployment_requests',deployment_requests,opts,topology,request,normalized);
  const sharedRequest=assembly.shared;
  if(sharedRequest.roles){const desired=new Set(declarations.map(n=>n.node_id));sharedRequest.peers=Object.fromEntries(Object.entries(observedNodes).filter(([id,peer])=>(operation==='delete'||desired.has(id))&&Object.hasOwn(sharedRequest.roles,peer.role)));}
  const sharedPlan=await call('provider_request',provider_request,opts,'shared',sharedRequest);
  doc=await snapshot();let existingShared:Map={};
  if(Object.values(doc.nodes).some((node:any)=>node.phase!=='destroyed'&&(operation==='delete'||!node.desired))&&doc.shared.phase!=='declared')existingShared=(await readable(keys.shared)).outputs;
  for(const [nodeId,node] of Object.entries(doc.nodes) as [string,Map][]){
   if(node.phase==='destroyed'||operation==='create'&&node.desired)continue;
   if(node.phase==='declared'){await attempt(nodeId,{},'delete');continue;}
   const nodeRequest:Map={...structuredClone(sharedRequest),node_id:nodeId};delete nodeRequest.name;if(node.role!=null){nodeRequest.role=node.role;if(nodeRequest.roles){require(Object.hasOwn(nodeRequest.roles,node.role));nodeRequest.security=structuredClone(nodeRequest.roles[node.role].security);}}
   const plan=await call('provider_request',provider_request,opts,'node',nodeRequest,existingShared);await attempt(nodeId,plan.documents,'delete');
  }
  if(operation==='delete'){
   await attempt(null,sharedPlan.documents,'delete');await coordinator.transition('key-cleanup');
   await call('cleanup_keypair',cleanupKeypair,opts,ownership,{all_resources_destroyed:true},env);
   await coordinator.transition('key-removed');await coordinator.transition('retire');return {status:'destroyed'};
  }
  const shared=await attempt(null,sharedPlan.documents,'create');const requests=Object.fromEntries(assembly.nodes.map((node:Map)=>[node.node_id,node]));
  const nodeStep=async(values:Map)=>{const nodeId=values['colors-compute/request'].node_id;
   try{const plan=await call('provider_request',provider_request,opts,'node',requests[nodeId],shared.outputs);const result=await attempt(nodeId,plan.documents,'create');return {...values,'colors-compute/params':{...result.params,...(key.private_key_path?{ssh_identity_file:key.private_key_path}:{})}};}
   catch(error){if(error instanceof Error&&error.name==='AbortError')throw error;return {...values,'red/exit':1,'red/err':'compute node failed'};}
  };
  const result=await call('run',run,clusterWorkflow(declarations,assembly.entry_node_id??declarations[0].node_id,nodeStep),opts);
  require(result['red/exit']===0&&result['colors-compute/cluster']);
  let sharedOutputs=shared.outputs;if(sharedRequest.roles){sharedRequest.peers=Object.fromEntries(result['colors-compute/cluster'].nodes.map((n:Map)=>[n.node_id,{role:n.role,vpc_ip:n.vpc_ip}]));const plan=await call('provider_request',provider_request,opts,'shared',sharedRequest);sharedOutputs=(await attempt(null,plan.documents,'create')).outputs;}
  return {status:'ready',cluster:result['colors-compute/cluster'],shared:sharedOutputs,key:Object.fromEntries(Object.entries(key).filter(([k])=>['mode','private_key_path','fingerprint'].includes(k)))};
 };
 let result:any,cancelled:any;
 try{result=await execute();}catch(error){if(error instanceof Error&&error.name==='AbortError')cancelled=error;result={status:'error'};}
 if(acquired)try{await coordinator.release();}catch(error){if(error instanceof Error&&error.name==='AbortError')cancelled=error;result={status:'error'};}
 if(cancelled)throw cancelled;return result;
}
