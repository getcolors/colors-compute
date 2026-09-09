import {chmodSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import executionPolicy from '../resources/execution-policy.json';
import templatesData from '../resources/templates.json';
import {registry} from './index.ts';
import {backend_plan} from './rendering.ts';
import {executeBackendCommand,parseStateEnvelope,stateOutputs,type BackendRunner} from './backend.ts';
import {session,writePrivate,serviceCode,etag,containsSecret,commonArgs} from './journal.ts';
type Map=Record<string,any>;type Env=Record<string,string|undefined>;
const object=(v:unknown):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const safe=(v:unknown)=>typeof v==='string'&&/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/.exec(v)?.[0]===v;
const missing=(v:unknown)=>typeof v!=='string'||!v.trim()||v.trim().toUpperCase()==='REPLACE_ME';
const cancelled=(error:unknown)=>error instanceof Error&&error.name==='AbortError';
const equal=(a:any,b:any):boolean=>a===b||(object(a)&&object(b)&&Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(key=>Object.hasOwn(b,key)&&equal(a[key],b[key])));
function stateKey(opts:Map,key:unknown):boolean {
  if(!safe(opts.profile)||typeof key!=='string')return false;
  if((key===`${opts.profile}/compute/shared.tfstate`||key===`${opts.profile}/compute/managed-kubernetes.tfstate`))return true;
  const prefix=`${opts.profile}/compute/nodes/`;
  return key.startsWith(prefix)&&key.endsWith('.tfstate')&&safe(key.slice(prefix.length,-8));
}
export async function statePresence(opts:Map,key:string,environment:Env=process.env,runner:BackendRunner=executeBackendCommand,legacy=false):Promise<{status:'present'|'absent'|'error'}> {
  if(!object(opts)||!(stateKey(opts,key)||(legacy===true&&safe(opts.profile)&&typeof key==='string'&&key.startsWith(opts.profile+'/')&&key.endsWith('.tfstate')&&safe(key.slice(opts.profile.length+1,-8)))))return {status:'error'};
  return session(opts,environment,async(directory,env,secrets,config)=>{
    const body=join(directory,'state.json');writePrivate(body,'');
    const args=['aws','s3api','get-object','--bucket',config.bucket,'--key',key,body,...commonArgs(config)];
    if(containsSecret(JSON.stringify(args),secrets))return {status:'error' as const};
    const result=await runner(args,{cwd:directory,env,timeoutMs:120000});
    if(result.exit!==0)return {status:serviceCode(result.err,'GetObject')==='NoSuchKey'?'absent' as const:'error' as const};
    const tag=etag(result.out);if(!tag||containsSecret(tag,secrets))return {status:'error' as const};
    return {status:'present' as const};
  });
}
const state=parseStateEnvelope;
const empty=(state:Map)=>state.resources.length===0&&Object.keys(state.outputs).length===0;
function validPlan(output:string,operation:string):boolean {
  let plan:unknown;try{plan=JSON.parse(output);}catch{return false;}
  if(!object(plan)||typeof plan.format_version!=='string'||!plan.format_version.trim()||!object(plan.planned_values))return false;
  const changes=Object.hasOwn(plan,'resource_changes')?plan.resource_changes:[];
  if(!Array.isArray(changes))return false;
  const permitted=operation==='create'?['no-op','read','create','update']:['no-op','read','delete'];
  return changes.every(resource=>object(resource)&&object(resource.change)&&Array.isArray(resource.change.actions)&&resource.change.actions.length===1&&permitted.includes(resource.change.actions[0]));
}
function jsonValue(value:unknown):boolean {
  if(value===null||typeof value==='string'||typeof value==='boolean')return true;
  if(typeof value==='number')return Number.isFinite(value);
  if(Array.isArray(value))return value.every(jsonValue);
  return object(value)&&Object.values(value).every(jsonValue);
}
function validDocuments(documents:unknown,provider:string):documents is Map {
  if(!object(documents)||!Object.keys(documents).length||!jsonValue(documents))return false;
  const templates=(templatesData as Map)[provider];if(!templates)return false;
  const namespaces=new Set<string>(),specs:Map={},providerFields:Map={},resourceTypes={resource:new Set<string>(),data:new Set<string>()};
  for(const stage of Object.values(templates) as Map[])for(const doc of Object.values(stage) as Map[]) {
    for(const [name,config] of Object.entries(doc.provider??{})){namespaces.add(name);providerFields[name]=new Set(Object.keys(config as Map));}
    Object.assign(specs,doc.terraform?.required_providers??{});
    for(const kind of ['resource','data'] as const)for(const name of Object.keys(doc[kind]??{}))resourceTypes[kind].add(name);
  }
  for(const [filename,document] of Object.entries(documents)) {
    if(/^[a-zA-Z0-9][a-zA-Z0-9_-]*\.tf\.json$/.exec(filename)?.[0]!==filename||filename==='backend.tf.json'||!object(document)||Object.keys(document).some(key=>!['terraform','provider','resource','data','locals','output'].includes(key)))return false;
    const terraform=Object.hasOwn(document,'terraform')?document.terraform:{},providers=Object.hasOwn(document,'provider')?document.provider:{};
    if(!object(terraform)||Object.hasOwn(terraform,'backend')||!object(providers)||Object.keys(providers).some(name=>!namespaces.has(name)))return false;
    const required=Object.hasOwn(terraform,'required_providers')?terraform.required_providers:{};
    if(!object(required)||Object.entries(required).some(([name,spec])=>!Object.hasOwn(specs,name)||!equal(spec,specs[name])))return false;
    if(Object.entries(providers).some(([name,config])=>!object(config)||Object.keys(config).some(field=>!providerFields[name].has(field))))return false;
    for(const kind of ['resource','data'] as const){const entries=Object.hasOwn(document,kind)?document[kind]:{};if(!object(entries)||Object.keys(entries).some(type=>!resourceTypes[kind].has(type)))return false;}
    const text=JSON.stringify(document);if(text.includes('-----BEGIN ')||text.includes('"private_key"')||text.includes('"provisioner"'))return false;
  }
  return true;
}
/** Internal lifecycle primitive: requires a confirmed schema-2 attempt and ownership. */
export async function convergeStateDecoded(opts:Map,key:string,documents:unknown,operation:string,presence:unknown,environment:Env=process.env,runner:BackendRunner=executeBackendCommand,sleeper:(milliseconds:number)=>Promise<unknown>=Bun.sleep,decoder?:(output:string)=>Map):Promise<Map> {
  let directory:string|undefined;
  try {
    if(!object(opts)||!stateKey(opts,key)||!['create','delete','check'].includes(operation)||!object(presence)||Object.keys(presence).length!==1||!['present','absent'].includes(presence.status))return {status:'error'};
    if(operation==='check'&&presence.status!=='present')return {status:'error'};
    const protect=Object.hasOwn(opts,'compute-prevent-destroy')?opts['compute-prevent-destroy']:true;
    if(typeof protect!=='boolean'||(operation==='delete'&&protect))return {status:'error'};
    const provider=opts['provider-compute'];if(typeof provider!=='string'||!Object.hasOwn(registry.compute,provider)||!validDocuments(documents,provider))return {status:'error'};
    const policy=(executionPolicy as Map)[provider]?.destroy_retry;const retry=operation==='delete'&&policy&&Object.values(documents).some(d=>Object.hasOwn(d.resource??{},policy.resource_type))?policy:null;
    const plan=backend_plan(opts,key);
    if(operation==='delete'&&presence.status==='absent')return {status:'destroyed'};
    const source={...environment},credentials:Map={},secrets:string[]=[];
    for(const [variable,option] of Object.entries(plan.credential_bindings)) {const value=source[variable];if(missing(value))return {status:'error'};credentials[option]=value;secrets.push(value!);}
    const env:Record<string,string>={};for(const [name,value] of Object.entries(source))if(value!==undefined&&!/^(TF_|TOFU_|COLORS_PAR_)/.test(name))env[name]=value;
    for(const [secret,variable] of Object.entries((registry.compute as Map)[provider]['tofu-env']) as [string,string][]) {
      const value=source['COLORS_PAR_'+secret.toUpperCase().replaceAll('-','_')];if(missing(value))return {status:'error'};env[variable]=value!;secrets.push(value!);
    }
    if(containsSecret(JSON.stringify(documents),secrets))return {status:'error'};
    directory=mkdtempSync(join(tmpdir(),'colors-compute-execution-'));chmodSync(directory,0o700);
    for(const [filename,document] of Object.entries(documents))writePrivate(join(directory,filename),JSON.stringify(document));
    writePrivate(join(directory,'backend.tf.json'),JSON.stringify(plan.config));
    const credentialFile=join(directory,'credentials.tfbackend.json');writePrivate(credentialFile,JSON.stringify(credentials));
    const planFile=join(directory,'approved.tfplan');writePrivate(planFile,'');
    Object.assign(env,{TF_IN_AUTOMATION:'1',TF_INPUT:'0',TF_WORKSPACE:'default',TF_DATA_DIR:join(directory,'.terraform')});
    const execute=async(args:string[],timeoutMs=120000):Promise<string>=>{
      const command=['tofu',...args];if(containsSecret(JSON.stringify(command),secrets))throw new Error('invalid command');
      const result=await runner(command,{cwd:directory!,env,timeoutMs});if(result.exit!==0){const error:any=new Error('execution failed');error.retryable=!!(retry&&args[0]==='apply'&&typeof result.err==='string'&&result.err.length<=1048576&&result.err.includes(retry.error_text)&&!containsSecret(result.err,secrets));throw error;}return result.out;
    };
    await execute(['init','-input=false','-no-color','-reconfigure',`-backend-config=${credentialFile}`]);
    const before=await execute(['state','pull']);
    if(before.trim()) {
      const current=state(before);
      if(empty(current.document)){if(operation==='delete')return {status:'destroyed'};}
      else if(current.params.provider!==provider)return {status:'error'};
    }else if(presence.status!=='absent')return {status:'error'};
    if(operation==='check'){if(!before.trim()||empty(state(before).document))return {status:'error'};await execute(['plan','-input=false','-no-color','-detailed-exitcode'],1800000);return {status:'clean'};}
    for(let attempt=0;attempt<(retry?.attempts??1);attempt++){
      const args=['plan','-input=false','-no-color',`-out=${planFile}`];if(operation==='delete')args.push('-destroy');
      await execute(args,1800000);if(!validPlan(await execute(['show','-json',planFile]),operation))return {status:'error'};
      try{await execute(['apply','-input=false','-no-color',planFile],1800000);break;}
      catch(error){if(!(error as any).retryable||attempt+1>=retry.attempts)throw error;const observed=state(await execute(['state','pull']));if(empty(observed.document))return {status:'destroyed'};if(observed.params.provider!==provider)return {status:'error'};await sleeper(retry.delay_ms);}
    }
    const after=await execute(['state','pull']);if(operation==='delete'&&!after.trim())return {status:'destroyed'};
    const final=state(after);
    if(operation==='delete')return {status:empty(final.document)?'destroyed':'error'};
    const outputs=decoder?decoder(after):stateOutputs(after);
    if(final.params.provider!==provider||containsSecret(JSON.stringify(outputs),secrets))return {status:'error'};
    return {status:'ready',params:final.params,outputs};
  }catch(error){if(cancelled(error))throw error;return {status:'error'};}
  finally {if(directory)try{rmSync(directory,{recursive:true,force:true});}catch{return {status:'error'};}}
}

export async function checkState(opts:Map,key:string,documents:unknown,environment:Env=process.env,runner:BackendRunner=executeBackendCommand):Promise<Map>{return convergeState(opts,key,documents,'check',{status:'present'},environment,runner);}

export async function convergeState(opts:Map,key:string,documents:unknown,operation:string,presence:unknown,environment:Env=process.env,runner:BackendRunner=executeBackendCommand,sleeper:(milliseconds:number)=>Promise<unknown>=Bun.sleep):Promise<Map>{return convergeStateDecoded(opts,key,documents,operation,presence,environment,runner,sleeper);}
