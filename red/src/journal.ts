import {chmodSync,mkdtempSync,readFileSync,rmSync,statSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {backend_plan} from './rendering.ts';
import {state_keys} from './index.ts';
import {identityValid,documentValid,identityEqual} from './coordination.ts';
import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;
type Env=Record<string,string|undefined>;
export type JournalGetResult={status:'present';etag:string;document:Map}|{status:'absent'|'error'};
export type JournalPutResult={status:'written';etag:string}|{status:'conflict'|'error'};
const object=(value:unknown):value is Map=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const nonblank=(value:unknown):value is string=>typeof value==='string'&&!!value.trim();
const exact=(value:unknown,keys:string[]):value is Map=>object(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));
const limit=2*1024*1024;
function configuration(opts:Map) {
  state_keys(opts.profile,[]);
  const key=`${opts.profile}/compute/coordination.json`;
  const plan=backend_plan(opts,key);
  const settings=plan.config.terraform.backend.s3;
  if(!nonblank(settings.bucket)||!nonblank(settings.region)) throw new Error('invalid settings');
  if(opts['provider-backend']==='r2'&&!nonblank(settings.endpoints.s3)) throw new Error('invalid settings');
  return {key,bucket:settings.bucket,region:settings.region,endpoint:settings.endpoints?.s3};
}
export function identity(opts:Map):Map {
  const kind=opts['provider-backend'];
  return {profile:opts.profile,provider:opts['provider-compute'],backend:{kind,bucket:opts[`${kind}-bucket`],region:kind==='r2'?'auto':opts['s3-region'],...(kind==='r2'?{endpoint:opts['r2-endpoint']}:{})}};
}
function secrets(opts:Map,environment:Env):string[] {
  if(opts['provider-backend']!=='r2')return [];
  return ['COLORS_PAR_R2_ACCESS_KEY_ID','COLORS_PAR_R2_SECRET_ACCESS_KEY'].map(name=>{
    const value=environment[name];
    if(!nonblank(value)||value.trim().toUpperCase()==='REPLACE_ME'||/[\r\n\u2028\u2029]/.test(value))throw new Error('invalid credentials');
    return value;
  });
}
export function containsSecret(text:string,values:string[]):boolean {
  return values.some(value=>text.includes(value)||text.includes(JSON.stringify(value).slice(1,-1)));
}
export function writePrivate(path:string,content:string) {writeFileSync(path,content,{mode:0o600,flag:'wx'});}
function childEnvironment(environment:Env,directory:string,values:string[]):Record<string,string> {
  const env:Record<string,string>={};
  for(const [key,value] of Object.entries(environment)) {
    if(value===undefined||key.startsWith('COLORS_PAR_'))continue;
    if(values.length&&key.startsWith('AWS_')&&key!=='AWS_CA_BUNDLE')continue;
    env[key]=value;
  }
  if(values.length) {
    const credentials=join(directory,'credentials');const config=join(directory,'config');
    writePrivate(credentials,`[default]\naws_access_key_id = ${values[0]}\naws_secret_access_key = ${values[1]}\n`);
    writePrivate(config,'');
    Object.assign(env,{AWS_SHARED_CREDENTIALS_FILE:credentials,AWS_CONFIG_FILE:config,AWS_REQUEST_CHECKSUM_CALCULATION:'when_required',AWS_RESPONSE_CHECKSUM_VALIDATION:'when_required'});
  }
  Object.assign(env,{AWS_PAGER:'',AWS_CLI_AUTO_PROMPT:'off',AWS_MAX_ATTEMPTS:'1'});
  return env;
}
export function serviceCode(stderr:string,operation:string):string|undefined {
  const match=/^\s*(?:aws: \[ERROR\]: )?An error occurred \(([A-Za-z0-9]+)\) when calling the ([A-Za-z0-9]+) operation(?: \(reached max retries: [0-9]+\))?:/.exec(stderr);
  return match?.[2]===operation?match[1]:undefined;
}
export function etag(stdout:string):string|undefined {
  const value:unknown=JSON.parse(stdout);
  return object(value)&&nonblank(value.ETag)?value.ETag:undefined;
}
export async function session<T>(opts:Map,environment:Env,action:(directory:string,env:Record<string,string>,values:string[],config:ReturnType<typeof configuration>)=>Promise<T>):Promise<T|{status:'error'}> {
  let directory:string|undefined;
  try {
    const config=configuration(opts);const values=secrets(opts,environment);
    directory=mkdtempSync(join(tmpdir(),'colors-compute-journal-'));chmodSync(directory,0o700);
    const env=childEnvironment(environment,directory,values);
    return await action(directory,env,values,config);
  }catch(error){
    if(error instanceof Error && error.name === 'AbortError')throw error;
    return {status:'error'};
  }
  finally {
    if(directory)try{rmSync(directory,{recursive:true,force:true});}catch{return {status:'error'};}
  }
}
export function commonArgs(config:ReturnType<typeof configuration>) {
  return ['--region',config.region,'--output','json','--no-cli-pager',...(config.endpoint?['--endpoint-url',config.endpoint]:[])];
}

/** Fetch untrusted journal data. Reducer validation is still required. */
export async function journalGet(opts:Map,environment:Env=process.env,runner:BackendRunner=executeBackendCommand):Promise<JournalGetResult> {
  return session<JournalGetResult>(opts,environment,async(directory,env,values,config)=>{
    const body=join(directory,'body.json');writePrivate(body,'');
    const args=['aws','s3api','get-object','--bucket',config.bucket,'--key',config.key,body,...commonArgs(config)];
    if(containsSecret(JSON.stringify(args),values))return {status:'error'};
    const result=await runner(args,{cwd:directory,env,timeoutMs:120_000});
    if(result.exit!==0)return {status:serviceCode(result.err,'GetObject')==='NoSuchKey'?'absent':'error'};
    const tag=etag(result.out);
    if(!tag||statSync(body).size>limit)return {status:'error'};
    const text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(readFileSync(body));
    if(containsSecret(text,values)||containsSecret(tag,values))return {status:'error'};
    const document:unknown=JSON.parse(text);
    if(!object(document))return {status:'error'};
    return {status:'present',etag:tag,document};
  });
}

/** Perform exactly one conditional journal write; errors are ambiguous. */
export async function journalPut(opts:Map,intent:unknown,environment:Env=process.env,runner:BackendRunner=executeBackendCommand):Promise<JournalPutResult> {
  // Input validation precedes session creation, credential writing and execution.
  if(!object(opts))return {status:'error'};
  const expected=identity(opts);
  if(!identityValid(expected)||!exact(intent,['condition','document'])||!documentValid(intent.document)||!identityEqual(intent.document.identity,expected))return {status:'error'};
  const condition=intent.condition;
  if(!((exact(condition,['if_none_match'])&&condition.if_none_match==='*')||(exact(condition,['if_match'])&&nonblank(condition.if_match))))return {status:'error'};
  let bodyText:string;
  try{bodyText=JSON.stringify(intent.document);}catch{return {status:'error'};}
  if(Buffer.byteLength(bodyText,'utf8')>limit)return {status:'error'};
  return session<JournalPutResult>(opts,environment,async(directory,env,values,config)=>{
    if(containsSecret(JSON.stringify(intent),values))return {status:'error'};
    const body=join(directory,'body.json');writePrivate(body,bodyText);
    const args=['aws','s3api','put-object','--bucket',config.bucket,'--key',config.key,'--body',body,'--content-type','application/json',...(Object.hasOwn(condition,'if_match')?['--if-match',condition.if_match]:['--if-none-match','*']),...commonArgs(config)];
    if(containsSecret(JSON.stringify(args),values))return {status:'error'};
    const result=await runner(args,{cwd:directory,env,timeoutMs:120_000});
    if(result.exit!==0)return {status:['PreconditionFailed','ConditionalRequestConflict'].includes(serviceCode(result.err,'PutObject')??'')?'conflict':'error'};
    const tag=etag(result.out);
    return tag&&!containsSecret(tag,values)?{status:'written',etag:tag}:{status:'error'};
  });
}
