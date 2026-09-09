import {identityValid,identityEqual} from './coordination.ts';
type Map=Record<string,any>;
const obj=(v:any):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const exact=(v:any,keys:string[]):v is Map=>obj(v)&&Object.keys(v).length===keys.length&&keys.every(k=>Object.hasOwn(v,k));
const safe=(v:any)=>typeof v==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/.exec(v)?.[0]===v;
const nonblank=(v:any)=>typeof v==='string'&&!!v.trim();
const integer=(v:any)=>Number.isSafeInteger(v)&&v>0;
const require=(v:any,message='managed transition refused')=>{if(!v)throw Error(message);};
export function managedDocumentValid(doc:any):doc is Map{
 if(!exact(doc,['schema_version','kind','identity','revision','write_id','lock','generation','status','shared'])||doc.schema_version!==3||doc.kind!=='managed-kubernetes'||!identityValid(doc.identity)||!integer(doc.revision)||!integer(doc.generation)||!safe(doc.write_id))return false;
 const lock=doc.lock,r=doc.shared;if(!exact(lock,['state','run_id'])||!(lock.state==='held'&&safe(lock.run_id)||lock.state==='idle'&&lock.run_id===null))return false;
 if(!['active','retired'].includes(doc.status)||!exact(r,['phase','operation','operation_id'])||!['declared','running','ready','failed','destroyed'].includes(r.phase))return false;
 if(r.phase==='declared'){if(r.operation!==null||r.operation_id!==null)return false;}else if(!['create','destroy'].includes(r.operation)||!safe(r.operation_id))return false;
 if(r.phase==='ready'&&r.operation!=='create'||r.phase==='destroyed'&&r.operation!=='destroy'||r.phase==='running'&&lock.state!=='held')return false;
 return doc.status!=='retired'||r.phase==='destroyed';
}
export function managedCoordination(observed:any,identity:any,event:any):Map{
 require(identityValid(identity),'invalid coordination identity');require(obj(event)&&typeof event.type==='string'&&event.type.startsWith('managed/'),'invalid managed event');const kind=event.type.slice(8);
 const extras:Map={acquire:[],release:[],recreate:[],'shared-start':['operation_id'],'shared-destroy':['operation_id'],'shared-complete':['operation_id'],'shared-fail':['operation_id'],'shared-retry':['evidence']};
 require(Object.hasOwn(extras,kind)&&exact(event,['type','run_id','write_id','target_etag',...extras[kind]])&&safe(event.run_id)&&safe(event.write_id)&&(event.target_etag===null||nonblank(event.target_etag)),'invalid managed event');
 if(Object.hasOwn(event,'operation_id'))require(safe(event.operation_id),'invalid managed event');if(kind==='shared-retry')require(event.evidence==='readable-state','invalid managed event');
 require(obj(observed)&&(observed.status==='present'?exact(observed,['status','etag','document'])&&nonblank(observed.etag):['absent','error'].includes(observed.status)&&exact(observed,['status'])),'invalid coordination observation');require(observed.status!=='error','coordination read failed');
 const present=observed.status==='present';let doc=observed.document;
 if(present){require(managedDocumentValid(doc),'invalid managed document');require(identityEqual(doc.identity,identity),'coordination identity mismatch');require(doc.write_id!==event.write_id,'coordination write_id reused');require(doc.revision<Number.MAX_SAFE_INTEGER,'coordination revision exhausted');}
 require(event.target_etag===(present?observed.etag:null),'stale coordination observation');
 if(!present){require(kind==='acquire','coordination object absent');return {condition:{if_none_match:'*'},document:{schema_version:3,kind:'managed-kubernetes',identity:structuredClone(identity),revision:1,write_id:event.write_id,lock:{state:'held',run_id:event.run_id},generation:1,status:'active',shared:{phase:'declared',operation:null,operation_id:null}}};}
 doc=structuredClone(doc);const r=doc.shared;
 if(kind==='acquire'){require(doc.lock.state==='idle','coordination lock held');doc.lock={state:'held',run_id:event.run_id};}
 else{
  require(doc.lock.state==='held'&&doc.lock.run_id===event.run_id,'coordination owner mismatch');
  if(kind==='release'){require(r.phase!=='running','coordination operations outstanding');doc.lock={state:'idle',run_id:null};}
  else if(kind==='recreate'){require(doc.status==='retired'&&doc.generation<Number.MAX_SAFE_INTEGER);Object.assign(doc,{status:'active',generation:doc.generation+1,shared:{phase:'declared',operation:null,operation_id:null}});}
  else if(['shared-start','shared-destroy'].includes(kind)){const operation=kind==='shared-start'?'create':'destroy',allowed=operation==='create'?['declared','ready']:['declared','ready','failed'];require(doc.status==='active'&&allowed.includes(r.phase)&&event.operation_id!==r.operation_id);Object.assign(r,{phase:'running',operation,operation_id:event.operation_id});}
  else if(['shared-complete','shared-fail'].includes(kind)){require(r.phase==='running'&&r.operation_id===event.operation_id);r.phase=kind==='shared-fail'?'failed':r.operation==='create'?'ready':'destroyed';if(r.phase==='destroyed')doc.status='retired';}
  else if(kind==='shared-retry'){require(doc.status==='active'&&r.phase==='failed'&&r.operation==='create');doc.shared={phase:'declared',operation:null,operation_id:null};}
 }
 doc.revision++;doc.write_id=event.write_id;require(managedDocumentValid(doc),'invalid managed transition result');return {condition:{if_match:observed.etag},document:doc};
}
