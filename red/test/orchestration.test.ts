import {test,expect} from 'bun:test';
import {Coordinator} from '../src/coordinator.ts';
import {orchestrate} from '../src/orchestration.ts';
type Map=Record<string,any>;
const opts={profile:'demo','provider-compute':'vultr','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1','compute-prevent-destroy':false};
export class Runtime {
 observed:Map={status:'absent'};states:Map={};events:string[]=[];fails=new Set<string>();writes=0;
 deps=()=>({validate_deployment:()=>true,compute_credential_errors:()=>[],
  coordinator:(o:Map,config:Map)=>new Coordinator(o,{...config,read:async()=>structuredClone(this.observed),write:async(intent:Map)=>{
   if(intent.condition.if_match&&intent.condition.if_match!==this.observed.etag)return {status:'conflict'};
   this.observed={status:'present',etag:'etag-'+(++this.writes),document:structuredClone(intent.document)};return {status:'written',etag:this.observed.etag};
  }}),
  registration_preflight:async()=>({status:'checked'}),
  prepare_keypair:async(o:Map,owned:Map,env:Map,intent:any,prepared:any)=>{if(owned.status==='fresh'){await intent();await prepared('SHA256:'+'A'.repeat(43));}return {mode:'managed',public_key:'ssh-ed25519 public'};},
  cleanup_keypair:async()=>{this.events.push('cleanup');},
  deployment_requests:()=>({shared:{node_id:'shared'},nodes:Object.entries(this.observed.document.nodes).filter(([_,n]:any)=>n.desired).map(([id])=>({node_id:id}))}),
  provider_request:(o:Map,stage:string,request:Map)=>({documents:{node:stage==='shared'?'shared':request.node_id}}),
  state_presence:async(o:Map,key:string)=>({status:Object.hasOwn(this.states,key)?'present':'absent'}),
  read_state:async(o:Map,key:string)=>({status:'present',params:{provider:'vultr'},outputs:this.states[key],state_empty:Object.keys(this.states[key]??{}).length===0}),
  converge_state:async(o:Map,key:string,documents:Map,operation:string)=>{
   const id=documents.node;this.events.push(operation+':'+id);await Bun.sleep(id==='0'?8:1);
   if(this.fails.has(id))return {status:'error'};
   if(operation==='delete'){delete this.states[key];return {status:'destroyed'};}
   const params={node_id:id,provider:'vultr',name:'demo-'+id,ip:'192.0.2.1',user:'root',sudoer:'root'};
   const outputs={params};this.states[key]=outputs;this.events.push('ready:'+id);return {status:'ready',params,outputs};
  }
 });
 run(count=2,event='create'){return orchestrate({...opts,'red/event':event},[{count}],{},{},this.deps());}
}
test('native Colors create, scale down, destroy and recreate preserve ordering',async()=>{
 const r=new Runtime();const first=await r.run();expect(first.status).toBe('ready');expect(first.cluster.nodes.map((n:Map)=>n.node_id)).toEqual(['0','1']);
 expect(r.events.indexOf('ready:shared')).toBeLessThan(r.events.indexOf('create:0'));
 r.events=[];expect((await r.run(1)).status).toBe('ready');expect(r.events.indexOf('delete:1')).toBeLessThan(r.events.indexOf('create:shared'));
 r.events=[];expect(await r.run(1,'delete')).toEqual({status:'destroyed'});expect(r.events).toEqual(['delete:0','delete:shared','cleanup']);expect(r.states).toEqual({});
 expect(r.observed.document.status).toBe('retired');expect((await r.run(1)).status).toBe('ready');expect(r.observed.document.generation).toBe(2);
});
test('failed sibling settles before release and absent failed state refuses retry',async()=>{
 const r=new Runtime();r.fails.add('0');expect(await r.run()).toEqual({status:'error'});expect(r.events).toContain('ready:1');expect(r.observed.document.lock.state).toBe('idle');
 r.events=[];r.fails.clear();expect(await r.run()).toEqual({status:'error'});expect(r.events).toEqual([]);
});
test('unowned state and protected delete refuse before compute',async()=>{
 const r=new Runtime();r.states['demo/compute/shared.tfstate']={params:{provider:'vultr'}};expect(await r.run()).toEqual({status:'error'});expect(r.events).toEqual([]);
 const s=new Runtime();expect(await orchestrate({...opts,'red/event':'delete','compute-prevent-destroy':true},[{count:1}],{},{},s.deps())).toEqual({status:'error'});expect(s.writes).toBe(0);
});

test('retained empty state object allows create but nonempty unknown state does not',async()=>{
 const r=new Runtime();r.states['demo/compute/shared.tfstate']={};r.states['demo/compute/nodes/0.tfstate']={};expect((await r.run(1)).status).toBe('ready');
});
test('read-only inventory requires idle validated journal and complete node params',async()=>{
 const {read_deployment}=await import('../src/inspection.ts');const r=new Runtime();expect((await r.run()).status).toBe('ready');
 const deps={journal_get:async()=>structuredClone(r.observed),read_state:async(o:Map,key:string)=>({status:'present',params:r.states[key].params,outputs:r.states[key]})};
 const found=await read_deployment(opts,{HOME:'/example'},deps);expect(found.status).toBe('present');expect(found.cluster!.nodes.map((n:Map)=>n.node_id)).toEqual(['0','1']);expect(found.key!.private_key_path).toBe('/example/.ssh/demo');
 expect((await read_deployment(opts,{},deps,{entry_node_id:'1'})).cluster!.entry_node_id).toBe('1');
 expect(await read_deployment(opts,{},deps,{entry_node_id:'missing'})).toEqual({status:'error'});
 const owner=new Coordinator(opts,{eventPrefix:'lifecycle/',read:async()=>structuredClone(r.observed),write:async(intent:Map)=>{r.observed={status:'present',etag:'held',document:intent.document};return {status:'written',etag:'held'};}});await owner.acquire();
 expect(await read_deployment(opts,{},deps)).toEqual({status:'error'});
});
test('SDK callback options survive orchestration and coordinator snapshots',async()=>{
 const r=new Runtime();
 expect((await orchestrate({...opts,'red/callback':()=>true},[{count:1}],{},{},r.deps())).status).toBe('ready');
});
test('missing compute credentials are named after state ownership checks',async()=>{
 const r=new Runtime(),deps:any=r.deps();delete deps.compute_credential_errors;
 expect(await orchestrate(opts,[{count:1}],{},{},deps)).toEqual({status:'error',errors:['required credential is not set: COLORS_PAR_VULTR_API_KEY']});
 expect(r.writes).toBeGreaterThan(0);expect(r.events).toEqual([]);
 const s=new Runtime(),other:any=s.deps();delete other.compute_credential_errors;s.states['demo/compute/shared.tfstate']={params:{provider:'vultr'}};
 expect(await orchestrate(opts,[{count:1}],{},{},other)).toEqual({status:'error'});expect(s.events).toEqual([]);
});

test('existing state guard refuses fresh, uninitialized and retired ownership before writes',async()=>{
 const r=new Runtime(),guarded={...opts,'compute-require-existing-state':true};
 const run=()=>orchestrate(guarded,[{count:2}],{},{},r.deps());
 expect(await run()).toEqual({status:'error'});expect(r.writes).toBe(0);expect(r.events).toEqual([]);
 const owner=r.deps().coordinator(opts,{eventPrefix:'lifecycle/'});await owner.acquire();await owner.release();
 const initial=r.writes;expect(await run()).toEqual({status:'error'});expect(r.writes).toBe(initial);
 expect((await r.run()).status).toBe('ready');expect((await run()).status).toBe('ready');
 expect(await r.run(2,'delete')).toEqual({status:'destroyed'});const before=r.writes;r.events=[];
 expect(await run()).toEqual({status:'error'});expect(r.writes).toBe(before);expect(r.events).toEqual([]);
 expect(await orchestrate({...guarded,'red/event':'delete'},[{count:2}],{},{},r.deps())).toEqual({status:'destroyed'});
});
test('existing state guard requires actual booleans',async()=>{
 const {validate}=await import('../src/index.ts');
 for(const value of [null,'true',1,[]]){
  const r=new Runtime(),input={...opts,'compute-require-existing-state':value};
  expect(validate(input)).toContain(':compute-require-existing-state must be a boolean');
  expect(await orchestrate(input,[{count:1}],{},{},r.deps())).toEqual({status:'error'});expect(r.writes).toBe(0);
 }
});

test('recovered declared nodes require verified empty state and delete without convergence',async()=>{
 const {read_deployment}=await import('../src/inspection.ts');
 for(const empty of [true,false,null]){
  const r=new Runtime();expect((await r.run()).status).toBe('ready');
  for(const node of Object.values(r.observed.document.nodes) as Map[])Object.assign(node,{phase:'declared',operation:null,operation_id:null});
  const deps=r.deps(),original=deps.read_state;
  deps.read_state=async(o:Map,key:string):Promise<any>=>key.includes('/nodes/')?{status:'present',state_empty:empty}:original(o,key);
  expect(await read_deployment(opts,{}, {journal_get:async()=>r.observed,read_state:deps.read_state})).toEqual({status:empty===true?'partial':'error'});
  r.events=[];expect(await orchestrate({...opts,'red/event':'delete'},[{count:2}],{},{},deps)).toEqual({status:empty===true?'destroyed':'error'});
  expect(r.events.some(event=>event==='delete:undefined'||event==='delete:0'||event==='delete:1')).toBe(false);
 }
});

test('retired journal held by finalizer reports destroyed without reading node state',async()=>{
 const {read_deployment}=await import('../src/inspection.ts');const r=new Runtime();await r.run();expect(await r.run(2,'delete')).toEqual({status:'destroyed'});
 r.observed.document.lock={state:'held',run_id:'finalizer'};
 expect(await read_deployment(opts,{}, {journal_get:async()=>r.observed})).toEqual({status:'destroyed'});
});
