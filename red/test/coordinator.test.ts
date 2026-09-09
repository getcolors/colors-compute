import {expect,test} from 'bun:test';
import {Coordinator} from '../src/coordinator.ts';
type Map=Record<string,any>;
const opts={profile:'demo','provider-compute':'vultr','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'};
class Store {
  observation:Map={status:'absent'};
  writes:Map[]=[];reads=0;fault:string|null=null;busy=0;maxBusy=0;
  read=async()=>{this.reads++;return structuredClone(this.observation);};
  write=async(intent:Map)=>{
    this.writes.push(structuredClone(intent));this.busy++;this.maxBusy=Math.max(this.maxBusy,this.busy);
    try {
      await Bun.sleep(1);
      const fault=this.fault;this.fault=null;
      if(fault==='abort')throw new DOMException('cancelled','AbortError');
      if(fault==='conflict')return {status:'conflict'};
      if(fault==='unknown')return {status:'unknown'};
      if(fault==='error')return {status:'error'};
      if(fault==='throw')throw new Error('synthetic-secret');
      if(('if_none_match' in intent.condition&&this.observation.status!=='absent')||('if_match' in intent.condition&&intent.condition.if_match!==this.observation.etag))return {status:'conflict'};
      this.observation={status:'present',etag:`etag-${this.writes.length}`,document:structuredClone(intent.document)};
      if(fault==='lost-error')return {status:'error'};
      if(fault==='lost-throw')throw new Error('synthetic-secret');
      return {status:'written',etag:this.observation.etag};
    } finally {this.busy--;}
  };
  coordinator(prefix='id'){let id=0;return new Coordinator(opts,{read:this.read,write:this.write,idFactory:()=>`${prefix}-${++id}`});}
}
test('construction has no IO and exactly one contender acquires',async()=>{
  const store=new Store();const a=store.coordinator('a'),b=store.coordinator('b');
  expect(store.reads).toBe(0);expect(store.writes).toHaveLength(0);expect(await a.snapshot()).toBeNull();
  const results=await Promise.allSettled([a.acquire(),b.acquire()]);
  expect(results.filter(result=>result.status==='fulfilled')).toHaveLength(1);
  expect(results.filter(result=>result.status==='rejected')).toHaveLength(1);
  expect(store.writes).toHaveLength(2);expect(store.observation.document.lock.state).toBe('held');
});
test('parallel starts and completions serialize CAS and retain failed siblings',async()=>{
  const store=new Store();const coordinator=store.coordinator();await coordinator.acquire();await coordinator.declare([{role:'broker',count:2}]);
  const ids=await Promise.all([coordinator.start('broker-0'),coordinator.start('broker-1')]);
  await expect(coordinator.release()).rejects.toThrow('coordination operations outstanding');
  await Promise.all([coordinator.complete('broker-1',ids[1]),coordinator.fail('broker-0',ids[0])]);
  expect(store.maxBusy).toBe(1);
  const snap=await coordinator.snapshot();expect(snap!.document.nodes['broker-0'].phase).toBe('failed');expect(snap!.document.nodes['broker-1'].phase).toBe('ready');
  await coordinator.release();expect(store.observation.document.lock.state).toBe('idle');
  await expect(coordinator.start('broker-0')).rejects.toThrow('coordination released');
});
test('lost committed responses confirm through one exact readback and never rewrite',async()=>{
  for(const fault of ['lost-error','lost-throw']) {
    const store=new Store();const coordinator=store.coordinator();store.fault=fault;await coordinator.acquire();
    expect(store.writes).toHaveLength(1);expect(store.reads).toBe(2);expect((await coordinator.snapshot())!.document.write_id).toBe('id-2');
    await coordinator.declare([{}]);expect(store.writes).toHaveLength(2);
  }
});
test('uncommitted ambiguous errors poison ownership without repeated writes',async()=>{
  for(const fault of ['error','throw']) {
    const store=new Store();const coordinator=store.coordinator();await coordinator.acquire();store.fault=fault;
    await expect(coordinator.declare([{}])).rejects.toThrow('coordination ownership uncertain');
    const writes=store.writes.length;await expect(coordinator.release()).rejects.toThrow('coordination ownership uncertain');expect(store.writes).toHaveLength(writes);expect(store.observation.document.lock.state).toBe('held');
  }
});
test('conflict and unknown result poison immediately with no readback',async()=>{
  for(const fault of ['conflict','unknown']) {
    const store=new Store();const coordinator=store.coordinator();await coordinator.acquire();store.fault=fault;
    await expect(coordinator.declare([{}])).rejects.toThrow('coordination ownership uncertain');expect(store.reads).toBe(1);expect(store.writes).toHaveLength(2);
    await expect(coordinator.release()).rejects.toThrow('coordination ownership uncertain');
  }
});
test('cancellation propagates and poisons without auto-release',async()=>{
  const store=new Store();const coordinator=store.coordinator();await coordinator.acquire();store.fault='abort';
  await expect(coordinator.declare([{}])).rejects.toThrow('cancelled');await expect(coordinator.release()).rejects.toThrow('coordination ownership uncertain');expect(store.writes).toHaveLength(2);expect(store.observation.document.lock.state).toBe('held');
});
test('reducer validation errors leave owned lock usable',async()=>{
  const store=new Store();const coordinator=store.coordinator();await coordinator.acquire();
  await expect(coordinator.declare([{count:0}])).rejects.toThrow('invalid coordination event');expect(store.writes).toHaveLength(1);
  await coordinator.declare([{}]);const id=await coordinator.start('0');
  await expect(coordinator.complete('0','wrong')).rejects.toThrow('coordination local attempt mismatch');await coordinator.complete('0',id);await coordinator.release();
});
test('finished local attempt is removed even when outcome cannot commit',async()=>{
  const store=new Store();const coordinator=store.coordinator();await coordinator.acquire();await coordinator.declare([{}]);const id=await coordinator.start('0');store.fault='error';
  await expect(coordinator.complete('0',id)).rejects.toThrow('coordination ownership uncertain');
  await expect(coordinator.complete('0',id)).rejects.toThrow('coordination local attempt mismatch');
  expect(store.observation.document.nodes['0'].phase).toBe('running');expect(store.observation.document.lock.state).toBe('held');
});
test('snapshot and queued inputs are detached and lifecycle misuse is refused',async()=>{
  const store=new Store();const coordinator=store.coordinator();
  await expect(coordinator.release()).rejects.toThrow('coordination not acquired');await coordinator.acquire();
  await expect(coordinator.acquire()).rejects.toThrow('coordination already acquired');
  const topology=[{role:'broker',count:1}];const declared=coordinator.declare(topology);topology[0].count=2;await declared;
  const snap=await coordinator.snapshot();snap!.document.nodes={};expect(Object.keys((await coordinator.snapshot())!.document.nodes)).toEqual(['broker-0']);
});
test('same run id cannot attach to held journal; IDs cannot repeat',async()=>{
  const store=new Store();const first=store.coordinator('same');await first.acquire();const sequence=['same-1','fresh-write'];const second=new Coordinator(opts,{read:store.read,write:store.write,idFactory:()=>sequence.shift()!});
  await expect(second.acquire()).rejects.toThrow('coordination lock held');
  const empty=new Store();const invalid=new Coordinator(opts,{read:empty.read,write:empty.write,idFactory:()=> 'repeated'});
  await expect(invalid.acquire()).rejects.toThrow('invalid coordination id');expect(empty.writes).toHaveLength(0);
});
test('readback must match every document field, not only write_id',async()=>{
  const store=new Store();let reads=0;let id=0;
  const coordinator=new Coordinator(opts,{idFactory:()=>`id-${++id}`,write:store.write,read:async()=>{
    const observed=await store.read();reads++;
    if(reads>1&&observed.status==='present')observed.document.revision++;
    return observed;
  }});
  store.fault='lost-error';await expect(coordinator.acquire()).rejects.toThrow('coordination ownership uncertain');
  expect(store.writes).toHaveLength(1);expect(await coordinator.snapshot()).toBeNull();
});
test('read callback exceptions are sanitized and poison ownership',async()=>{
  const coordinator=new Coordinator(opts,{read:async()=>{throw new Error('synthetic-secret');},write:async()=>{throw new Error('must not write');}});
  await expect(coordinator.acquire()).rejects.toThrow('coordination ownership uncertain');
  await expect(coordinator.acquire()).rejects.toThrow('coordination ownership uncertain');
});
test('schema2 coordinator tracks shared and node attempts through retirement',async()=>{
  const store=new Store();let nextId=0;
  const coordinator=new Coordinator(opts,{eventPrefix:'lifecycle/',read:store.read,write:store.write,idFactory:()=>`life-${++nextId}`});
  await coordinator.acquire();await coordinator.declare([{count:1}]);
  await coordinator.transition('key-intent',{mode:'external'});await coordinator.transition('key-prepared',{fingerprint:null});
  const shared=await coordinator.sharedStart();await expect(coordinator.release()).rejects.toThrow('coordination operations outstanding');await coordinator.sharedComplete(shared);
  const node=await coordinator.start('0');await coordinator.complete('0',node);
  await coordinator.transition('begin-delete');const destroy=await coordinator.destroy('0');await coordinator.complete('0',destroy);
  const sharedDestroy=await coordinator.sharedDestroy();await coordinator.sharedComplete(sharedDestroy);
  await coordinator.transition('key-cleanup');await coordinator.transition('key-removed');await coordinator.transition('retire');await coordinator.release();
  const snapshot=await coordinator.snapshot();expect(snapshot!.document.schema_version).toBe(2);expect(snapshot!.document.status).toBe('retired');expect(snapshot!.document.lock.state).toBe('idle');
});
test('generic transitions cannot bypass attempt tracking or terminal state',async()=>{
  const store=new Store();const coordinator=new Coordinator(opts,{eventPrefix:'lifecycle/',read:store.read,write:store.write});await coordinator.acquire();
  for(const name of ['start','destroy','shared-start','complete','release','acquire'])await expect(coordinator.transition(name)).rejects.toThrow('invalid coordination transition');
  await expect(coordinator.transition('key-intent',{mode:'external',run_id:'other'})).rejects.toThrow('invalid coordination transition');
  expect(store.writes).toHaveLength(1);
});
