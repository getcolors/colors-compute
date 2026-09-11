import {copy} from './copy.ts';
import {randomUUID} from 'node:crypto';
import {coordination,identityValid} from './coordination.ts';
import {journalGet,journalPut} from './journal.ts';
type Map=Record<string,any>;
export interface CoordinatorOptions {
  environment?:Record<string,string|undefined>;
  read?:()=>Promise<unknown>;
  write?:(intention:Map)=>Promise<unknown>;
  idFactory?:()=>string;
  eventPrefix?: '' | 'lifecycle/' | 'managed/';
  reducer?: typeof coordination;
}
const object=(value:unknown):value is Map=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const exact=(value:unknown,keys:string[]):value is Map=>object(value)&&keys.length===Object.keys(value).length&&keys.every(key=>Object.hasOwn(value,key));
const nonblank=(value:unknown)=>typeof value==='string'&&!!value.trim();
const cancelled=(error:unknown)=>error instanceof Error&&error.name==='AbortError';
function same(a:any,b:any):boolean {
  if(a===b)return true;
  if(Array.isArray(a)||Array.isArray(b))return Array.isArray(a)&&Array.isArray(b)&&a.length===b.length&&a.every((value,index)=>same(value,b[index]));
  return object(a)&&object(b)&&Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(key=>Object.hasOwn(b,key)&&same(a[key],b[key]));
}
/** One process-local journal owner. A confirmed start is required before dispatch. */
export class Coordinator {
  private readonly identity:Map;
  private readonly read:()=>Promise<unknown>;
  private readonly write:(intention:Map)=>Promise<unknown>;
  private readonly idFactory:()=>string;
  private readonly eventPrefix:string;
  private readonly reducer:typeof coordination;
  private readonly ids=new Set<string>();
  private readonly active=new Map<string,string>();
  private queue:Promise<void>=Promise.resolve();
  private observation:Map|null=null;
  private attempted=false;
  private acquired=false;
  private released=false;
  private poisoned=false;
  private runId:string|null=null;
  constructor(opts:Map,options:CoordinatorOptions={}) {
    const saved=copy(opts);
    const environment={...(options.environment??process.env)};
    const kind=saved['provider-backend'];
    this.identity={profile:saved.profile,provider:saved['provider-compute'],backend:{kind,bucket:saved[`${kind}-bucket`],region:kind==='r2'?'auto':saved[`${kind}-region`],...(kind==='oci'?{endpoint:`https://${saved['oci-namespace']}.compat.objectstorage.${saved['oci-region']}.oraclecloud.com`}:{}),...(kind==='r2'?{endpoint:saved['r2-endpoint']}:{})}};
    if(!identityValid(this.identity))throw new Error('invalid coordination identity');
    this.read=options.read??(()=>journalGet(saved,environment));
    this.write=options.write??(intention=>journalPut(saved,intention,environment));
    this.idFactory=options.idFactory??randomUUID;
    this.eventPrefix=options.eventPrefix??'';
    if(this.eventPrefix!==''&&this.eventPrefix!=='lifecycle/'&&this.eventPrefix!=='managed/')throw new Error('invalid coordination event prefix');
    this.reducer=options.reducer??coordination;
  }
  private serial<T>(operation:()=>Promise<T>|T):Promise<T> {
    const result=this.queue.then(operation);
    this.queue=result.then(()=>undefined,()=>undefined);
    return result;
  }
  private id():string {
    const value=this.idFactory();
    if(typeof value!=='string'||/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/.exec(value)?.[0]!==value||this.ids.has(value))throw new Error('invalid coordination id');
    this.ids.add(value);return value;
  }
  private uncertain(error?:unknown):never {
    this.poisoned=true;
    if(cancelled(error))throw error;
    throw new Error('coordination ownership uncertain');
  }
  private owner():void {
    if(this.poisoned)this.uncertain();
    if(this.released)throw new Error('coordination released');
    if(!this.acquired)throw new Error('coordination not acquired');
  }
  private async commit(observed:Map,event:Map):Promise<void> {
    // Reducer errors occur before transport and do not poison ownership.
    const intention=this.reducer(observed,this.identity,event);
    let response:unknown;
    try{response=await this.write(structuredClone(intention));}
    catch(error){if(cancelled(error))this.uncertain(error);response={status:'error'};}
    if(exact(response,['status','etag'])&&response.status==='written'&&nonblank(response.etag)) {
      this.observation={status:'present',etag:response.etag,document:structuredClone(intention.document)};return;
    }
    if(exact(response,['status'])&&response.status==='error') {
      let readback:unknown;
      try{readback=await this.read();}catch(error){this.uncertain(error);}
      if(exact(readback,['status','etag','document'])&&readback.status==='present'&&nonblank(readback.etag)&&same(readback.document,intention.document)) {
        this.observation=structuredClone(readback);return;
      }
    }
    this.uncertain();
  }
  private event(type:string,extra:Map={}):Map {
    return {type:this.eventPrefix+type,run_id:this.runId,write_id:this.id(),target_etag:this.observation!.etag,...extra};
  }
  acquire(requireExisting=false):Promise<void> {
    return this.serial(async()=>{
      if(this.poisoned)this.uncertain();
      if(this.attempted)throw new Error('coordination already acquired');
      this.attempted=true;this.runId=this.id();
      let observed:unknown;
      try{observed=await this.read();}catch(error){this.uncertain(error);}
      if(!(exact(observed,['status'])&&observed.status==='absent')&&!(exact(observed,['status','etag','document'])&&observed.status==='present'&&nonblank(observed.etag)))this.uncertain();
      if(typeof requireExisting!=='boolean')throw new Error('existing compute ownership required');
      if(requireExisting){const doc=observed.document;
        if(!(observed.status==='present'&&doc?.status==='active'&&doc.key?.phase==='prepared'&&['ready','failed'].includes(doc.shared?.phase)&&Object.values(doc.nodes??{}).some((node:any)=>['ready','failed'].includes(node.phase))))throw new Error('existing compute ownership required');}
      await this.commit(observed,{type:this.eventPrefix+'acquire',run_id:this.runId,write_id:this.id(),target_etag:observed.status==='present'?observed.etag:null});
      this.acquired=true;
    });
  }
  declare(topology:Map[]):Promise<void> {
    const saved=structuredClone(topology);
    return this.serial(async()=>{this.owner();await this.commit(this.observation!,this.event('declare',{topology:saved}));});
  }
  private begin(type:string,nodeId?:string):Promise<string> {
    return this.serial(async()=>{
      this.owner();const operationId=this.id();
      await this.commit(this.observation!,this.event(type,{...(nodeId===undefined?{}:{node_id:nodeId}),operation_id:operationId}));
      this.active.set(nodeId??'@shared',operationId);return operationId;
    });
  }
  start(nodeId:string):Promise<string>{return this.begin('start',nodeId);}
  destroy(nodeId:string):Promise<string>{return this.begin('destroy',nodeId);}
  sharedStart():Promise<string>{return this.begin('shared-start');}
  sharedDestroy():Promise<string>{return this.begin('shared-destroy');}
  private finish(type:string,nodeId:string|undefined,operationId:string):Promise<void> {
    return this.serial(async()=>{
      // Caller asserts the matching child terminated, even when ownership was lost.
      const key=nodeId??'@shared';
      if(this.active.get(key)!==operationId)throw new Error('coordination local attempt mismatch');
      this.active.delete(key);this.owner();
      await this.commit(this.observation!,this.event(type,{...(nodeId===undefined?{}:{node_id:nodeId}),operation_id:operationId}));
    });
  }
  complete(nodeId:string,operationId:string):Promise<void>{return this.finish('complete',nodeId,operationId);}
  fail(nodeId:string,operationId:string):Promise<void>{return this.finish('fail',nodeId,operationId);}
  sharedComplete(operationId:string):Promise<void>{return this.finish('shared-complete',undefined,operationId);}
  sharedFail(operationId:string):Promise<void>{return this.finish('shared-fail',undefined,operationId);}
  transition(name:string,payload:Map={}):Promise<void> {
    const saved=structuredClone(payload);
    return this.serial(async()=>{
      this.owner();
      if(!['key-intent','key-prepared','key-cleanup','key-removed','begin-delete','retire','recreate','retry','shared-retry'].includes(name))throw new Error('invalid coordination transition');
      if(!object(saved)||['type','run_id','write_id','target_etag'].some(key=>Object.hasOwn(saved,key)))throw new Error('invalid coordination transition');
      await this.commit(this.observation!,this.event(name,saved));
    });
  }
  release():Promise<void> {
    return this.serial(async()=>{
      this.owner();if(this.active.size)throw new Error('coordination operations outstanding');
      await this.commit(this.observation!,this.event('release'));this.released=true;
    });
  }
  snapshot():Promise<Map|null>{return this.serial(()=>structuredClone(this.observation));}
}
