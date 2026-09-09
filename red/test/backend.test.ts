import {expect,test} from 'bun:test';
import {existsSync,readFileSync,statSync,mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {readState,executeBackendCommand,type BackendRunner} from '../src/backend.ts';
const opts = {'provider-backend':'r2','r2-bucket':'states','r2-endpoint':'https://example.invalid'};
const env = {COLORS_PAR_R2_ACCESS_KEY_ID:'synthetic-access',COLORS_PAR_R2_SECRET_ACCESS_KEY:'synthetic-secret',AWS_PROFILE:'invalid-profile',AWS_DEFAULT_PROFILE:'invalid-default',AWS_ACCESS_KEY_ID:'ambient-access',AWS_SECRET_ACCESS_KEY:'ambient-secret',TF_LOG:'TRACE',TF_CLI_ARGS:'-lock=false',TF_WORKSPACE:'other',TF_VAR_password:'synthetic',TOFU_CLI_CONFIG_FILE:'/unsafe',COLORS_PAR_OTHER:'unrelated',PATH:process.env.PATH};
const valid = {version:4,serial:0,lineage:'lineage',resources:[],outputs:{params:{value:{provider:'aws',node_id:'0'}}}};
function reply(out: unknown=valid, exit=0) {return {exit,out:typeof out === 'string' ? out : JSON.stringify(out),err:''};}
test('R2 state reader isolates credentials, environment and cache and cleans up',async()=>{
  let directory = ''; const commands:string[][]=[];
  const runner: BackendRunner = async(args, options)=>{
    directory=options.cwd;commands.push(args);
    expect(statSync(directory).mode&0o777).toBe(0o700);
    for (const file of ['credentials.tfbackend.json','backend.tf.json']) expect(statSync(join(directory,file)).mode&0o777).toBe(0o600);
    expect(JSON.parse(readFileSync(join(directory,'credentials.tfbackend.json'),'utf8')).secret_key).toBe('synthetic-secret');
    expect(JSON.stringify(args)).not.toContain('synthetic');
    expect(options.env.AWS_PROFILE).toBeUndefined();
    expect(options.env.AWS_DEFAULT_PROFILE).toBeUndefined();
    expect(options.env.AWS_ACCESS_KEY_ID).toBe('ambient-access');
    expect(options.env.AWS_SECRET_ACCESS_KEY).toBe('ambient-secret');
    expect(Object.keys(options.env).filter(key=>/^(TF_|TOFU_|COLORS_PAR_)/.test(key)).sort()).toEqual(['TF_DATA_DIR','TF_INPUT','TF_IN_AUTOMATION','TF_WORKSPACE']);
    expect(options.env.TF_WORKSPACE).toBe('default');
    expect(options.timeoutMs).toBe(120_000);
    expect(readFileSync(join(directory,'backend.tf.json'),'utf8')).not.toContain('synthetic');
    expect(options.env.TF_DATA_DIR).toBe(join(directory,'.terraform'));
    return reply();
  };
  expect(await readState(opts,'demo/node.tfstate',env,runner)).toEqual({status:'present',params:{provider:'aws',node_id:'0'}});
  expect(commands.map(args=>args.slice(0,3))).toEqual([['tofu','init','-input=false'],['tofu','state','pull']]);
  expect(existsSync(directory)).toBe(false);
  expect(env.TF_LOG).toBe('TRACE');
});
test('S3 requires no COLORS_PAR credentials and retains ambient AWS settings',async()=>{
  const result = await readState({'provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'},'demo/state', {AWS_PROFILE:'selected'},async(args,options)=>{
    expect(options.env.AWS_PROFILE).toBe('selected');
    expect(JSON.parse(readFileSync(join(options.cwd,'credentials.tfbackend.json'),'utf8')).access_key).toBeUndefined();
    return reply({...valid,outputs:{}});
  });
  expect(result).toEqual({status:'present',params:{}});
});
test('init failures including negative exit never advance or leak diagnostics',async()=>{
  for(const exit of [-1,1,127]) {
    let calls=0;let directory='';
    const result=await readState(opts,'demo/state',env,async(args,options)=>{calls++;directory=options.cwd;return {exit,out:'synthetic-secret',err:'synthetic-secret'};});
    expect(result).toEqual({status:'error'});expect(calls).toBe(1);expect(existsSync(directory)).toBe(false);
  }
});
test('state failures or malformed documents never imply absent state',async()=>{
  for(const output of ['', 'invalid', {}, {...valid,version:3}, {...valid,serial:-1}, {...valid,serial:1.5}, {...valid,lineage:' '}, {...valid,outputs:[]}, {...valid,resources:{}}, {...valid,outputs:{params:{value:null}}}]){
    const result=await readState(opts,'demo/state',env,async args=>args[1]==='init'?reply():reply(output));
    expect(result).toEqual({status:'error'});
  }
});
test('missing credentials and thrown errors remain generic and clean up',async()=>{
  let calls=0;expect(await readState(opts,'demo/state',{},async()=>{calls++;return reply();})).toEqual({status:'error'});expect(calls).toBe(0);
  let directory='';expect(await readState(opts,'demo/state',env,async(args,options)=>{directory=options.cwd;throw new Error('synthetic-secret');})).toEqual({status:'error'});expect(existsSync(directory)).toBe(false);
});
test('bound backend credentials cannot escape in state params',async()=>{
  for (const value of ['prefix-synthetic-secret-suffix','synthetic-access']) {
    const state={...valid,outputs:{params:{value:{metadata:value}}}};
    expect(await readState(opts,'demo/state',env,async args=>args[1]==='init'?reply():reply(state))).toEqual({status:'error'});
  }
});
test('native runner executes fixed commands with an exact sanitized environment',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'colors-tofu-stub-'));
  try {
    writeFileSync(join(directory,'tofu'),`#!/bin/sh
[ -z "$TF_LOG" ] || exit 91
[ -z "$TF_CLI_ARGS" ] || exit 92
[ "$AWS_PROFILE" = "ambient" ] || exit 93
[ "$TF_WORKSPACE" = "default" ] || exit 94
if [ "$1" = "init" ]; then exit 0; fi
if [ "$1" = "state" ] && [ "$2" = "pull" ]; then
printf '%s' '{"version":4,"serial":0,"lineage":"fixture","resources":[],"outputs":{}}'
exit 0
fi
exit 95
`,{mode:0o700});
    expect(await readState({'provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'},'demo/state',{PATH:directory,AWS_PROFILE:'ambient',TF_LOG:'TRACE',TF_CLI_ARGS:'-lock=false'})).toEqual({status:'present',params:{}});
  } finally {rmSync(directory,{recursive:true,force:true});}
});
test.skipIf(process.platform === 'win32')('timeout kills descendants holding inherited output pipes',async()=>{
  const started=Date.now();
  await expect(executeBackendCommand(['/bin/sh','-c','/bin/sleep 30 & exit 0'],{cwd:tmpdir(),env:{},timeoutMs:100})).rejects.toThrow('backend command timed out');
  expect(Date.now()-started).toBeLessThan(3000);
});
test('optional flattened shared outputs reject sensitive values and backend secret echoes',async()=>{
  const output={...valid,outputs:{...valid.outputs,ssh_key_id:{value:'shared-key',sensitive:false}}};
  expect(await readState(opts,'demo/state',env,async()=>reply(output),true)).toEqual({status:'present',params:valid.outputs.params.value,outputs:{params:valid.outputs.params.value,ssh_key_id:'shared-key'},state_empty:false});
  for(const entry of [{value:'key',sensitive:true},{value:'synthetic-secret'},{value:'key',sensitive:null},{}]){
    expect(await readState(opts,'demo/state',env,async()=>reply({...output,outputs:{...output.outputs,ssh_key_id:entry}}),true)).toEqual({status:'error'});
  }
});
