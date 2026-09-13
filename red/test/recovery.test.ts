import {test,expect} from 'bun:test';
import {recover_absent_aws_shared,commit_reviewed_repair} from '../src/recovery.ts';
const opts={profile:'demo','provider-compute':'aws','provider-backend':'s3','s3-bucket':'states','s3-region':'us-east-1','aws-region':'us-east-1'};
function owner(){return {doc:{status:'active',key:{phase:'prepared'},shared:{phase:'failed',operation:'create',operation_id:'attempt'},nodes:{'0':{phase:'declared'}}},transitions:[] as any[],released:false,async acquire(){},async snapshot(){return {document:this.doc};},async transition(...args:any[]){this.transitions.push(args);},async release(){this.released=true;}};}
const runner=(count:number)=>async(args:string[])=>args[1]==='s3api'?{exit:1,out:'',err:'An error occurred (NoSuchKey) when calling the GetObject operation: missing'}:{exit:0,out:String(count),err:''};
test('explicit failed attempt recovery records scanned absence',async()=>{const o=owner();expect(await recover_absent_aws_shared(opts,'attempt',{},runner(0),()=>o)).toEqual({status:'recovered'});expect(o.transitions).toEqual([['shared-retry',{evidence:'verified-provider-absence'}]]);expect(o.released).toBe(true);});
test('surviving resources and wrong attempt refuse recovery',async()=>{for(const [id,count] of [['attempt',1],['wrong',0]] as const){const o=owner();await expect(recover_absent_aws_shared(opts,id,{},runner(count),()=>o)).rejects.toThrow();expect(o.transitions).toEqual([]);expect(o.released).toBe(true);}});

const ropts={profile:'demo','provider-compute':'vultr','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'};
const rnode=(i:number,phase:string,op:string|null,oid:string|null)=>({state_key:`demo/compute/nodes/${i}.tfstate`,role:null,index:i,desired:true,phase,operation:op,operation_id:oid});
const rdoc={schema_version:2,identity:{profile:'demo',provider:'vultr',backend:{kind:'s3',bucket:'states',region:'eu-west-1'}},revision:7,write_id:'write-7',lock:{state:'held',run_id:'run-dead'},generation:1,status:'active',topology_declared:true,key:{mode:'managed',phase:'prepared',fingerprint:'SHA256:'+'A'.repeat(43)},shared:{phase:'ready',operation:'create',operation_id:'op-shared'},nodes:{'0':rnode(0,'running','create','op-0')}};
const robserved={status:'present',etag:'etag-7',document:rdoc};
const rdeclared={nodes:{'0':rnode(0,'declared',null,null)}};
function rjournal(reads:any[]){const puts:any[]=[];let i=0;return {puts,deps:{write_id:'write-8',journal_get:async()=>reads[Math.min(i++,reads.length-1)],journal_put:async(_o:any,intent:any)=>{puts.push(intent);return {status:'written',etag:'etag-8'};}}};}
test('reviewed repair commits with precondition and read-back',async()=>{
 const expected={...structuredClone(rdoc),nodes:rdeclared.nodes,lock:{state:'idle',run_id:null},revision:8,write_id:'write-8'};
 const j=rjournal([robserved,{status:'present',etag:'etag-8',document:expected}]);
 expect(await commit_reviewed_repair(ropts,{},robserved,'run-dead',rdeclared,j.deps)).toEqual({status:'written',etag:'etag-8'});
 expect(j.puts).toEqual([{condition:{if_match:'etag-7'},document:expected}]);
});
test('reviewed repair refuses stale or unconfirmed writes',async()=>{
 let j=rjournal([{...robserved,etag:'etag-moved'}]);await expect(commit_reviewed_repair(ropts,{},robserved,'run-dead',rdeclared,j.deps)).rejects.toThrow('lifecycle stale observation');expect(j.puts).toEqual([]);
 j=rjournal([robserved,robserved]);await expect(commit_reviewed_repair(ropts,{},robserved,'run-dead',rdeclared,j.deps)).rejects.toThrow('repair not confirmed');expect(j.puts.length).toBe(1);
 j=rjournal([robserved]);await expect(commit_reviewed_repair(ropts,{},robserved,'run-other',rdeclared,j.deps)).rejects.toThrow('lifecycle owner mismatch');expect(j.puts).toEqual([]);
});
