import {expect,test} from 'bun:test';
import {createHash} from 'node:crypto';
import {mkdtempSync,mkdirSync,readFileSync,rmSync,statSync,symlinkSync,writeFileSync,existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {backend_plan,credential_requirements,coordination,statePresence,convergeState,readState} from '../src/index.ts';
import {identity,journalGet,journalPut} from '../src/journal.ts';
import {identityEqual,identityValid} from '../src/coordination.ts';
import {localJournalPut} from '../src/local.ts';
const root=()=>mkdtempSync(join(tmpdir(),'colors-local-test-'));
const options=(path:string)=>({profile:'demo','provider-compute':'vultr','provider-backend':'local','local-state-dir':path});
const key='demo/compute/nodes/0.tfstate';
test('local backend validates normalized paths and binds no credentials',()=>{
  for(const path of ['relative','/tmp/','/tmp//state','/tmp/../state','/tmp/./state','/tmp\\state','/tmp\0state',42])expect(()=>backend_plan(options(path as string),key)).toThrow(':local-state-dir must be an absolute normalized POSIX path');
  expect(backend_plan(options('/'),key)).toEqual({config:{terraform:{backend:{local:{path:'/'+key}}}},credential_bindings:{},environment:{}});
  expect(credential_requirements({...options('/tmp/state'),'provider-compute':'aws'})).toEqual([]);
  expect(identityValid(identity(options('/tmp/state')))).toBe(true);
  expect(identityEqual(identity(options('/tmp/a')),identity(options('/tmp/b')))).toBe(false);
});
test('local journal writes use byte hashes, conditional replacement and a shared lock',async()=>{
 const directory=root(),opts=options(directory),file=join(directory,'demo/compute/coordination.json');
 try{
  const intent=coordination({status:'absent'},identity(opts),{type:'acquire',run_id:'run',write_id:'write',target_etag:null});
  const first=await journalPut(opts,intent,{});expect(first.status).toBe('written');
  expect((first as any).etag).toBe(createHash('sha256').update(readFileSync(file)).digest('hex'));
  expect(statSync(file).mode&0o777).toBe(0o600);expect(statSync(join(directory,'demo/compute')).mode&0o777).toBe(0o700);
  expect(await journalPut(opts,intent,{})).toEqual({status:'conflict'});
  const observed=await journalGet(opts,{}),release=coordination(observed,identity(opts),{type:'release',run_id:'run',write_id:'next',target_etag:(observed as any).etag});
  mkdirSync(file+'.lock');expect(await journalPut(opts,release,{})).toEqual({status:'conflict'});rmSync(file+'.lock',{recursive:true});
  expect((await journalPut(opts,release,{})).status).toBe('written');expect(await journalPut(opts,release,{})).toEqual({status:'conflict'});
  for(const bad of ['[]','{','\ufeff{}','{"x":NaN}']){writeFileSync(file,bad);expect(await journalGet(opts,{})).toEqual({status:'error'});}
 }finally{rmSync(directory,{recursive:true,force:true});}
});
test('local state distinguishes absence from invalid filesystem entries',async()=>{
 const directory=root(),opts=options(directory),file=join(directory,key);
 try{
  expect(await statePresence(opts,key,{})).toEqual({status:'absent'});
  mkdirSync(join(directory,'demo/compute/nodes'),{recursive:true});symlinkSync(join(directory,'missing'),file);
  expect(await statePresence(opts,key,{})).toEqual({status:'error'});expect(await readState(opts,key,{})).toEqual({status:'error'});
  rmSync(file);mkdirSync(file);expect(await statePresence(opts,key,{})).toEqual({status:'error'});
  rmSync(file,{recursive:true});writeFileSync(file,'{}');expect(await statePresence(opts,key,{})).toEqual({status:'present'});
 }finally{rmSync(directory,{recursive:true,force:true});}
});
test('local execution keeps state after its temporary context is removed',async()=>{
 const directory=root(),opts=options(directory),file=join(directory,key),calls:string[]=[];let temporary='';
 const state={version:4,serial:1,lineage:'fixture',resources:[{}],outputs:{params:{value:{provider:'vultr'}}}};
 try{
  const result=await convergeState(opts,key,{'node.tf.json':{resource:{vultr_instance:{node:{label:'demo'}}}}},'create',{status:'absent'},{COLORS_PAR_VULTR_API_KEY:'fixture-token'},async(args,context)=>{
   temporary=context.cwd;calls.push(args[1]);expect(JSON.parse(readFileSync(join(context.cwd,'backend.tf.json'),'utf8')).terraform.backend.local.path).toBe(file);
   if(args[1]==='apply'){writeFileSync(file,JSON.stringify(state),{mode:0o644});writeFileSync(file+'.backup','{}',{mode:0o644});}
   return {exit:0,err:'',out:args[1]==='show'?JSON.stringify({format_version:'1.2',planned_values:{},resource_changes:[{change:{actions:['create']}}]}):args[1]==='state'?JSON.stringify(state):''};
  });
  expect(result.status).toBe('ready');expect(calls).toEqual(['init','plan','show','apply','state']);expect(existsSync(temporary)).toBe(false);expect(existsSync(file)).toBe(true);expect(statSync(file).mode&0o777).toBe(0o600);expect(statSync(file+'.backup').mode&0o777).toBe(0o600);
 }finally{rmSync(directory,{recursive:true,force:true});}
});

test('journal refuses a symlink ancestor before changing its target',()=>{
 const directory=root(),target=join(directory,'target');
 try{
  mkdirSync(target,{mode:0o755});symlinkSync(target,join(directory,'link'));
  expect(localJournalPut(join(directory,'link/journal.json'),{if_none_match:'*'},'{}')).toEqual({status:'error'});
  expect(statSync(target).mode&0o777).toBe(0o755);expect(existsSync(join(target,'journal.json'))).toBe(false);
 }finally{rmSync(directory,{recursive:true,force:true});}
});

test('local execution removes temporary credentials when state protection fails',async()=>{
 const directory=root(),opts=options(directory),file=join(directory,key);let temporary='';
 try{
  const result=await convergeState(opts,key,{'node.tf.json':{resource:{vultr_instance:{node:{label:'demo'}}}}},'create',{status:'absent'},{COLORS_PAR_VULTR_API_KEY:'fixture-token'},async(_args,context)=>{
   temporary=context.cwd;symlinkSync(join(directory,'missing'),file);
   return {exit:0,out:'',err:''};
  });
  expect(result).toEqual({status:'error'});expect(temporary).not.toBe('');expect(existsSync(temporary)).toBe(false);
 }finally{rmSync(directory,{recursive:true,force:true});}
});

test('local journal protects existing profile directories without changing the configured root',async()=>{
 const directory=root(),opts=options(directory);
 try{
  mkdirSync(join(directory,'demo'),{mode:0o755});mkdirSync(join(directory,'demo/compute'),{mode:0o755});
  const mode=statSync(directory).mode&0o777;
  const intent=coordination({status:'absent'},identity(opts),{type:'acquire',run_id:'run',write_id:'write',target_etag:null});
  expect((await journalPut(opts,intent,{})).status).toBe('written');
  expect(statSync(join(directory,'demo')).mode&0o777).toBe(0o700);expect(statSync(join(directory,'demo/compute')).mode&0o777).toBe(0o700);expect(statSync(directory).mode&0o777).toBe(mode);
 }finally{rmSync(directory,{recursive:true,force:true});}
});
