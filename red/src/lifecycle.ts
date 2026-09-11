import {identityValid,identityEqual} from './coordination.ts';
import {expand,state_keys} from './index.ts';
type Map=Record<string,any>;
const object=(v:unknown):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const fields=(v:unknown,keys:string[],optional:string[]=[]):v is Map=>object(v)&&keys.every(k=>Object.hasOwn(v,k))&&Object.keys(v).every(k=>[...keys,...optional].includes(k));
const safe=(v:unknown)=>typeof v==='string'&&/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/.exec(v)?.[0]===v;
const nonblank=(v:unknown)=>typeof v==='string'&&!!v.trim();
const integer=(v:unknown,min:number,max=Number.MAX_SAFE_INTEGER)=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=min&&v<=max;
const roleValid=(v:unknown)=>v===null||(typeof v==='string'&&/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.exec(v)?.[0]===v);
const fingerprint=(v:unknown)=>typeof v==='string'&&/^SHA256:[A-Za-z0-9+/]{43}$/.exec(v)?.[0]===v;
const running=(record:Map)=>record.phase==='running'||record.phase==='destroying';
const idleShared=()=>({phase:'declared',operation:null,operation_id:null});
function topology(value:unknown):Map[]|null {
  if(!Array.isArray(value)||!value.length||value.length>1000)return null;
  let total=0;
  for(const entry of value) {
    if(!fields(entry,[],['role','count'])||!roleValid(entry.role??null)||!integer(entry.count===undefined?1:entry.count,1,1000))return null;
    total+=entry.count??1;if(total>1000)return null;
  }
  try{return expand(value);}catch{return null;}
}
function recordValid(record:Map):boolean {
  if(!['declared','running','ready','failed','destroying','destroyed'].includes(record.phase))return false;
  if(record.phase==='declared')return record.operation===null&&record.operation_id===null;
  if(!safe(record.operation_id)||!['create','destroy'].includes(record.operation))return false;
  if(['running','ready'].includes(record.phase))return record.operation==='create';
  if(['destroying','destroyed'].includes(record.phase))return record.operation==='destroy';
  return true;
}
export function lifecycleDocumentValid(value:unknown):value is Map {
  if(!fields(value,['schema_version','identity','revision','write_id','lock','generation','status','topology_declared','key','shared','nodes'])||value.schema_version!==2||!identityValid(value.identity)||!integer(value.revision,1)||!integer(value.generation,1)||!safe(value.write_id)||!['active','deleting','retired'].includes(value.status)||typeof value.topology_declared!=='boolean')return false;
  if(!fields(value.lock,['state','run_id'])||!((value.lock.state==='held'&&safe(value.lock.run_id))||(value.lock.state==='idle'&&value.lock.run_id===null)))return false;
  const key=value.key;
  if(!fields(key,['mode','phase','fingerprint'])||!['absent','intent','prepared','cleanup','removed'].includes(key.phase))return false;
  if(key.phase==='absent') {if(key.mode!==null||key.fingerprint!==null)return false;}
  else {
    if(!['managed','external'].includes(key.mode))return false;
    if(key.mode==='external'||key.phase==='intent'){if(key.fingerprint!==null)return false;}
    else if(!fingerprint(key.fingerprint))return false;
  }
  if(!fields(value.shared,['phase','operation','operation_id'])||!recordValid(value.shared)||!object(value.nodes)||Object.keys(value.nodes).length>1000)return false;
  const ops=new Set<string>();if(value.shared.operation_id!==null)ops.add(value.shared.operation_id);
  const desiredRoles=new Map<string|null,number[]>();
  if(running(value.shared)&&value.lock.state!=='held')return false;
  for(const [id,node] of Object.entries(value.nodes)) {
    if(!safe(id)||!fields(node,['state_key','role','index','desired','phase','operation','operation_id'])||!roleValid(node.role)||!integer(node.index,0,999)||typeof node.desired!=='boolean'||!recordValid(node))return false;
    if(id!==(node.role===null?String(node.index):`${node.role}-${node.index}`)||node.state_key!==`${value.identity.profile}/compute/nodes/${id}.tfstate`)return false;
    if(node.operation_id!==null){if(ops.has(node.operation_id))return false;ops.add(node.operation_id);}
    if(running(node)&&value.lock.state!=='held')return false;
    if(node.desired)desiredRoles.set(node.role,[...(desiredRoles.get(node.role)??[]),node.index]);
  }
  const allDestroyed=[value.shared,...Object.values(value.nodes)].every((record:any)=>record.phase==='destroyed');
  const allUndesired=Object.values(value.nodes).every((node:any)=>!node.desired);
  if(value.status==='deleting'&&!allUndesired)return false;
  if(value.status==='retired'&&(value.key.phase!=='removed'||!allDestroyed||!allUndesired))return false;
  if(['cleanup','removed'].includes(value.key.phase)&&(!['deleting','retired'].includes(value.status)||!allDestroyed))return false;
  if(desiredRoles.has(null)&&desiredRoles.size!==1)return false;
  if(![...desiredRoles.values()].every(indices=>indices.sort((a,b)=>a-b).every((index,position)=>index===position)))return false;
  return true;
}
function eventValid(event:unknown):event is Map {
  if(!object(event)||typeof event.type!=='string'||!event.type.startsWith('lifecycle/'))return false;
  const name=event.type.slice(10);
  const extras:Record<string,string[]>={acquire:[],release:[],'begin-delete':[],retire:[],recreate:[],declare:['topology'],'key-intent':['mode'],'key-prepared':['fingerprint'],'key-cleanup':[],'key-removed':[],'shared-start':['operation_id'],'shared-destroy':['operation_id'],'shared-complete':['operation_id'],'shared-fail':['operation_id'],'shared-retry':['evidence'],start:['node_id','operation_id'],destroy:['node_id','operation_id'],complete:['node_id','operation_id'],fail:['node_id','operation_id'],retry:['node_id','evidence']};
  if(!Object.hasOwn(extras,name)||!fields(event,['type','run_id','write_id','target_etag',...extras[name]])||!safe(event.run_id)||!safe(event.write_id)||!(event.target_etag===null||nonblank(event.target_etag)))return false;
  if(Object.hasOwn(event,'node_id')&&!safe(event.node_id))return false;
  if(Object.hasOwn(event,'operation_id')&&!safe(event.operation_id))return false;
  if(name==='declare'&&!topology(event.topology))return false;
  if(name==='key-intent'&&!['managed','external'].includes(event.mode))return false;
  if(name==='key-prepared'&&event.fingerprint!==null&&!fingerprint(event.fingerprint))return false;
  if(Object.hasOwn(event,'evidence')&&event.evidence!=='readable-state'&&!(['shared-retry','retry'].includes(name)&&event.evidence==='verified-provider-absence'))return false;
  return true;
}
function fail(message:string):never{throw new Error(message);}
function requireTransition(condition:unknown):asserts condition{if(!condition)fail('lifecycle transition refused');}

/** Schema-2 transition plan only; transport must confirm each intent. */
export function lifecycle(observation:unknown,identity:unknown,event:unknown) {
  if(!identityValid(identity))fail('invalid lifecycle identity');
  if(!eventValid(event))fail('invalid lifecycle event');
  if(!object(observation)||!(['absent','error'].includes(observation.status)?fields(observation,['status']):observation.status==='present'&&fields(observation,['status','etag','document'])&&nonblank(observation.etag)))fail('invalid lifecycle observation');
  if(observation.status==='error')fail('lifecycle read failed');
  const present=observation.status==='present',doc=observation.document,name=event.type.slice(10);
  if(present&&!lifecycleDocumentValid(doc))fail('invalid lifecycle document');
  if(present&&!identityEqual(doc.identity,identity))fail('lifecycle identity mismatch');
  if(event.target_etag!==(present?observation.etag:null))fail('lifecycle stale observation');
  if(present&&event.write_id===doc.write_id)fail('lifecycle write_id reused');
  if(present&&doc.revision===Number.MAX_SAFE_INTEGER)fail('lifecycle revision exhausted');
  if(!present) {
    requireTransition(name==='acquire');
    return {condition:{if_none_match:'*'},document:{schema_version:2,identity:structuredClone(identity),revision:1,write_id:event.write_id,lock:{state:'held',run_id:event.run_id},generation:1,status:'active',topology_declared:false,key:{mode:null,phase:'absent',fingerprint:null},shared:idleShared(),nodes:{}}};
  }
  const next:Map=structuredClone(doc);const records=()=>[next.shared,...Object.values(next.nodes)] as Map[];
  const activeWork=()=>records().some(running);const allDestroyed=()=>records().every(record=>record.phase==='destroyed');
  const unique=()=>records().every(record=>record.operation_id!==event.operation_id);
  if(name==='acquire') {
    if(next.lock.state==='held')fail('lifecycle lock held');next.lock={state:'held',run_id:event.run_id};
  }else {
    if(next.lock.state!=='held')fail('lifecycle lock not held');if(next.lock.run_id!==event.run_id)fail('lifecycle owner mismatch');
    switch(name) {
      case 'declare': {
        requireTransition(next.status==='active'&&!activeWork());
        const requested=topology(event.topology)!;const ids=new Set(requested.map(node=>node.node_id));
        requireTransition(new Set([...Object.keys(next.nodes),...ids]).size<=1000);
        for(const node of Object.values(next.nodes) as Map[])node.desired=false;
        const keys=state_keys(identity.profile,requested.map(node=>node.node_id));
        for(const node of requested){
          const old=next.nodes[node.node_id];
          next.nodes[node.node_id]=old&&old.phase!=='destroyed'?{...old,desired:true}:{state_key:keys.nodes[node.node_id],role:node.role,index:node.index,desired:true,...idleShared()};
        }
        next.topology_declared=true;break;
      }
      case 'key-intent':requireTransition(next.status==='active'&&next.key.phase==='absent');next.key={mode:event.mode,phase:'intent',fingerprint:null};break;
      case 'key-prepared':requireTransition(next.key.phase==='intent'&&(next.key.mode==='managed'?fingerprint(event.fingerprint):event.fingerprint===null));next.key.phase='prepared';next.key.fingerprint=event.fingerprint;break;
      case 'shared-start':requireTransition(next.status==='active'&&next.topology_declared&&next.key.phase==='prepared'&&!activeWork()&&['declared','ready'].includes(next.shared.phase)&&unique());next.shared={phase:'running',operation:'create',operation_id:event.operation_id};break;
      case 'shared-destroy':requireTransition((Object.values(next.nodes) as Map[]).every(node=>node.phase==='destroyed')&&!['running','destroying','destroyed'].includes(next.shared.phase)&&unique());next.shared={phase:'destroying',operation:'destroy',operation_id:event.operation_id};break;
      case 'shared-complete':case 'shared-fail':requireTransition(running(next.shared)&&next.shared.operation_id===event.operation_id);next.shared.phase=name==='shared-fail'?'failed':next.shared.operation==='create'?'ready':'destroyed';break;
      case 'shared-retry':requireTransition(next.shared.phase==='failed'&&next.shared.operation==='create');next.shared=idleShared();break;
      case 'start':case 'destroy': {
        requireTransition(Object.hasOwn(next.nodes,event.node_id)&&unique());const node=next.nodes[event.node_id];
        if(name==='start')requireTransition(next.status==='active'&&node.desired&&next.key.phase==='prepared'&&next.shared.phase==='ready'&&['declared','ready'].includes(node.phase));
        else requireTransition(!node.desired&&!['running','destroying','destroyed'].includes(node.phase));
        node.phase=name==='start'?'running':'destroying';node.operation=name==='start'?'create':'destroy';node.operation_id=event.operation_id;break;
      }
      case 'complete':case 'fail': {requireTransition(Object.hasOwn(next.nodes,event.node_id));const node=next.nodes[event.node_id];requireTransition(running(node)&&node.operation_id===event.operation_id);node.phase=name==='fail'?'failed':node.operation==='create'?'ready':'destroyed';break;}
      case 'retry': {requireTransition(Object.hasOwn(next.nodes,event.node_id));const node=next.nodes[event.node_id];requireTransition(node.phase==='failed'&&node.operation==='create');Object.assign(node,idleShared());break;}
      case 'begin-delete':requireTransition(next.status!=='retired'&&!activeWork());next.status='deleting';for(const node of Object.values(next.nodes) as Map[])node.desired=false;break;
      case 'key-cleanup':requireTransition(next.status==='deleting'&&allDestroyed()&&next.key.phase==='prepared');next.key.phase='cleanup';break;
      case 'key-removed':requireTransition(next.key.phase==='cleanup');next.key.phase='removed';break;
      case 'retire':requireTransition(next.status==='deleting'&&next.key.phase==='removed'&&allDestroyed());next.status='retired';break;
      case 'recreate':requireTransition(next.status==='retired'&&!activeWork()&&next.generation<Number.MAX_SAFE_INTEGER);next.generation++;next.status='active';next.key={mode:null,phase:'absent',fingerprint:null};next.shared=idleShared();next.topology_declared=false;break;
      case 'release':requireTransition(!activeWork()&&!['intent','cleanup'].includes(next.key.phase));next.lock={state:'idle',run_id:null};break;
      default:requireTransition(false);
    }
  }
  next.revision++;next.write_id=event.write_id;
  return {condition:{if_match:observation.etag},document:next};
}
