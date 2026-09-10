import {expect,test} from 'bun:test';
import {existsSync,readFileSync,readdirSync,statSync} from 'node:fs';
import {join} from 'node:path';
import {convergeState,statePresence} from '../src/execution.ts';
import type {BackendRunner} from '../src/backend.ts';
const opts={profile:'demo','provider-compute':'vultr','provider-backend':'r2','r2-bucket':'states','r2-endpoint':'https://example.invalid'};
const env={AWS_PROFILE:'compute-profile',AWS_SESSION_TOKEN:'ambient-session',AWS_ACCESS_KEY_ID:'ambient-access',AWS_SECRET_ACCESS_KEY:'ambient-secret',COLORS_PAR_R2_ACCESS_KEY_ID:'fixture-r2-access',COLORS_PAR_R2_SECRET_ACCESS_KEY:'fixture-r2-secret',COLORS_PAR_VULTR_API_KEY:'fixture-vultr-token',TF_CLI_ARGS:'-lock=false',TF_LOG:'TRACE'};
const key='demo/compute/nodes/0.tfstate';
const documents={'node.tf.json':{resource:{vultr_instance:{node:{label:'demo-0'}}}}};
const state={version:4,serial:1,lineage:'fixture',resources:[{type:'vultr_instance'}],outputs:{params:{value:{provider:'vultr',ip:'192.0.2.1'}}}};
const empty={...state,resources:[],outputs:{}};
const plan={format_version:'1.2',planned_values:{},resource_changes:[{change:{actions:['create']}}]};
export class Runner {
  calls:string[][]=[];directories:string[]=[];
  constructor(public before='',public after=JSON.stringify(state),public planned:any=plan,public failure=''){}
  run:BackendRunner=async(args,options)=>{
    this.calls.push(args);this.directories.push(options.cwd);
    expect(statSync(options.cwd).mode&0o777).toBe(0o700);
    for(const file of readdirSync(options.cwd)){const path=join(options.cwd,file);if(statSync(path).isFile())expect(statSync(path).mode&0o777).toBe(0o600);}
    expect(options.env.AWS_PROFILE).toBe(env.AWS_PROFILE);expect(options.env.AWS_SESSION_TOKEN).toBe(env.AWS_SESSION_TOKEN);expect(options.env.AWS_ACCESS_KEY_ID).toBe(env.AWS_ACCESS_KEY_ID);
    expect(options.env.VULTR_API_KEY).toBe(env.COLORS_PAR_VULTR_API_KEY);expect(options.env.TF_LOG).toBeUndefined();expect(options.env.TF_CLI_ARGS).toBeUndefined();expect(Object.keys(options.env).some(key=>key.startsWith('COLORS_PAR_'))).toBe(false);
    expect(JSON.parse(readFileSync(join(options.cwd,'credentials.tfbackend.json'),'utf8'))).toEqual({access_key:env.COLORS_PAR_R2_ACCESS_KEY_ID,secret_key:env.COLORS_PAR_R2_SECRET_ACCESS_KEY});
    expect(options.timeoutMs).toBe(['plan','apply'].includes(args[1])?1800000:120000);
    expect(JSON.stringify(args)).not.toContain('fixture-');
    if(args[1]===this.failure)return {exit:-1,out:'',err:'secret-failure'};
    if(args[1]==='state')return {exit:0,out:this.calls.some(command=>command[1]==='apply')?this.after:this.before,err:''};
    return {exit:0,out:args[1]==='show'?JSON.stringify(this.planned):'',err:''};
  };
}
test('new node executes fixed guarded sequence with separated credentials',async()=>{
  const runner=new Runner();
  expect(await convergeState(opts,key,documents,'create',{status:'absent'},env,runner.run)).toEqual({status:'ready',params:state.outputs.params.value,outputs:{params:state.outputs.params.value}});
  expect(runner.calls.map(command=>command[1])).toEqual(['init','state','plan','show','apply','state']);expect(runner.calls[4].at(-1)).toBe(runner.calls[3].at(-1));expect(existsSync(runner.directories[0])).toBe(false);
});
test('present empty, malformed, legacy or mismatched state refuses before plan',async()=>{
  for(const before of ['', 'invalid',JSON.stringify({...state,outputs:{}}),JSON.stringify({...state,outputs:{params:{value:{provider:'aws'}}}})]){
    const runner=new Runner(before);expect(await convergeState(opts,key,documents,'create',{status:'present'},env,runner.run)).toEqual({status:'error'});expect(runner.calls.some(command=>command[1]==='plan')).toBe(false);expect(existsSync(runner.directories[0])).toBe(false);
  }
});
test('replacements and unknown plan actions never apply',async()=>{
  for(const actions of [['delete','create'],['delete'],['create','delete'],['unknown'],[],null]){
    const runner=new Runner('',undefined,{...plan,resource_changes:[{change:{actions}}]});expect(await convergeState(opts,key,documents,'create',{status:'absent'},env,runner.run)).toEqual({status:'error'});expect(runner.calls.some(command=>command[1]==='apply')).toBe(false);
  }
});
test('every nonzero process exit fails closed and cleans up',async()=>{
  for(const failure of ['init','state','plan','show','apply']){
    const runner=new Runner('',undefined,undefined,failure);expect(await convergeState(opts,key,documents,'create',{status:'absent'},env,runner.run)).toEqual({status:'error'});expect(existsSync(runner.directories[0])).toBe(false);
  }
});
test('post-apply outputs must validate and contain no bound credentials or sensitive values',async()=>{
  for(const after of ['', '{}',JSON.stringify(empty),JSON.stringify({...state,outputs:{...state.outputs,secret:{value:'fixture-vultr-token'}}}),JSON.stringify({...state,outputs:{...state.outputs,secret:{value:'value',sensitive:true}}})]){
    const runner=new Runner('',after);expect(await convergeState(opts,key,documents,'create',{status:'absent'},env,runner.run)).toEqual({status:'error'});
  }
  const runner=new Runner('',JSON.stringify({...state,outputs:{...state.outputs,ssh_key_id:{value:'shared-key',sensitive:false}}}));expect((await convergeState(opts,key,documents,'create',{status:'absent'},env,runner.run)).outputs.ssh_key_id).toBe('shared-key');
});
test('delete is guarded and confirmed absent state needs no subprocess',async()=>{
  let calls=0;const never:BackendRunner=async()=>{calls++;return {exit:1,out:'',err:''};};
  expect(await convergeState(opts,key,documents,'delete',{status:'absent'},env,never)).toEqual({status:'error'});
  const deleting={...opts,'compute-prevent-destroy':false};expect(await convergeState(deleting,key,documents,'delete',{status:'absent'},env,never)).toEqual({status:'destroyed'});expect(calls).toBe(0);
  const runner=new Runner(JSON.stringify(state),JSON.stringify(empty),{...plan,resource_changes:[{change:{actions:['delete']}}]});expect(await convergeState(deleting,key,documents,'delete',{status:'present'},env,runner.run)).toEqual({status:'destroyed'});expect(runner.calls[2]).toContain('-destroy');
});
test('arbitrary state keys or document provider overrides are refused before execution',async()=>{
  let calls=0;const never:BackendRunner=async()=>{calls++;return {exit:0,out:'',err:''};};
  for(const wrong of ['other/compute/shared.tfstate','demo/other','demo/compute/nodes/../x.tfstate']){expect(await statePresence(opts,wrong,env,never)).toEqual({status:'error'});expect(await convergeState(opts,wrong,documents,'create',{status:'absent'},env,never)).toEqual({status:'error'});}
  for(const invalid of [ {'../bad.tf.json':{}},{'backend.tf.json':{}},{'node.tf.json':{provider:{evil:{}}}},{'node.tf.json':{provider:{vultr:{api_key:'secret'}}}},{'node.tf.json':{terraform:{required_providers:{vultr:{source:'evil/vultr',version:'1'}}}}},{'node.tf.json':{resource:{null_resource:{node:{}}}}},{'node.tf.json':{resource:{vultr_instance:{node:{provisioner:{}}}}}},{'node.tf.json':{provider:null}},{'node.tf.json':{locals:{number:NaN}}}])expect(await convergeState(opts,key,invalid,'create',{status:'absent'},env,never)).toEqual({status:'error'});
  expect(calls).toBe(0);
});
test('presence recognizes only exact missing object and cancellation cleans sessions',async()=>{
  let directory='';
  expect(await statePresence(opts,key,env,async(args,options)=>{directory=options.cwd;expect(args[6]).toBe(key);expect(options.env.AWS_PROFILE).toBeUndefined();return {exit:255,out:'',err:'aws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (reached max retries: 0): missing'};})).toEqual({status:'absent'});expect(existsSync(directory)).toBe(false);
  expect(await statePresence(opts,key,env,async()=>({exit:0,out:'{"ETag":"opaque"}',err:''}))).toEqual({status:'present'});
  await expect(convergeState(opts,key,documents,'create',{status:'absent'},env,async(args,options)=>{directory=options.cwd;throw new DOMException('cancelled','AbortError');})).rejects.toThrow('cancelled');expect(existsSync(directory)).toBe(false);
});

export {opts as executionOpts,env as executionEnv,key as executionKey,documents as executionDocuments,state as executionState,empty as emptyState};
test('virgin backend state requires confirmed absence',async()=>{
 const virgin={version:4,terraform_version:'1.12.5',serial:0,lineage:'',resources:[],outputs:{}};
 const runner=new Runner(JSON.stringify(virgin));expect((await convergeState(opts,key,documents,'create',{status:'absent'},env,runner.run)).status).toBe('ready');
 for(const [observed,s] of [['present',virgin],['absent',{...virgin,serial:1}],['absent',{...virgin,resources:[{}]}],['absent',{...virgin,outputs:{foreign:{}}}]] as const){const r=new Runner(JSON.stringify(s));expect(await convergeState(opts,key,documents,'create',{status:observed},env,r.run)).toEqual({status:'error'});expect(r.calls.some(c=>c[1]==='plan')).toBe(false);}
});
