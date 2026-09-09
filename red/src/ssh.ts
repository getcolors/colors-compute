import {chmodSync,closeSync,existsSync,fstatSync,lstatSync,mkdirSync,openSync,readFileSync,unlinkSync} from 'node:fs';
import {homedir} from 'node:os';
import {resolve,join} from 'node:path';
import {createHash} from 'node:crypto';
import {registry} from './index.ts';
import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;type Env=Record<string,string|undefined>;
export class SSHError extends Error{}
function refuse(message:string):never{throw new SSHError(message);}
const object=(v:unknown):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const nonblank=(v:unknown):v is string=>typeof v==='string'&&!!v.trim()&&v.trim().toUpperCase()!=='REPLACE_ME';
const safe=(v:unknown)=>typeof v==='string'&&/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/.exec(v)?.[0]===v;
const cancelled=(e:unknown)=>e instanceof Error&&e.name==='AbortError';
export function mode(opts:Map):Map {
  if(!safe(opts.profile))refuse(':profile must be a safe identifier');
  const provider=opts['provider-compute'];if(typeof provider!=='string'||!Object.hasOwn(registry.compute,provider))refuse('invalid SSH compute provider');
  const entry=(registry.compute as Map)[provider],settings=[entry['ssh-setting'],...Object.keys(entry['ssh-aliases']??{})].filter(s=>Object.hasOwn(opts,s));
  if(settings.length>1)refuse('ambiguous external SSH key settings');if(!settings.length)return {mode:'managed'};const setting=settings[0];
  const value=opts[setting];if(Object.hasOwn(entry['ssh-aliases']??{},setting)&&!nonblank(value))refuse('invalid external SSH key reference');
  if(!(nonblank(value)||(Array.isArray(value)&&value.length&&value.every(v=>nonblank(v)||(typeof v==='number'&&Number.isSafeInteger(v)&&v>0)))))refuse('invalid external SSH key reference');
  const result:Map={mode:'external',setting,reference:structuredClone(value)};
  const identitySetting=Object.hasOwn(opts,'ssh-private-key-path')?'ssh-private-key-path':provider+'-ssh-private-key';
  if(Object.hasOwn(opts,identitySetting)){if(!nonblank(opts[identitySetting]))refuse('invalid external SSH identity reference');result.private_key_path=opts[identitySetting];}
  return result;
}
function ownership(value:unknown):asserts value is Map {
  if(object(value)&&Object.keys(value).length===1&&value.status==='fresh')return;
  if(object(value)&&Object.keys(value).length===2&&value.status==='prepared'&&typeof value.fingerprint==='string'&&/^SHA256:[A-Za-z0-9+/]{43}$/.exec(value.fingerprint)?.[0]===value.fingerprint)return;
  refuse('SSH key ownership uncertain');
}
function paths(opts:Map,env:Env):string[]{const directory=resolve(nonblank(env.HOME)?env.HOME:homedir(),'.ssh');return [directory,join(directory,opts.profile),join(directory,opts.profile+'.pub'),join(directory,opts.profile+'.known_hosts')];}
function exists(path:string):boolean{try{lstatSync(path);return true;}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return false;throw error;}}
function safePaths(paths:string[]):void {paths.forEach((path,index)=>{if(exists(path)){const stat=lstatSync(path);if(stat.isSymbolicLink()||!(index===0?stat.isDirectory():stat.isFile()))refuse('unsafe SSH key path');}});}
async function reservation<T>(directory:string,profile:string,operation:()=>Promise<T>):Promise<T> {
  const lock=join(directory,'.'+profile+'.colors-key.lock');let descriptor:number;
  try{descriptor=openSync(lock,'wx',0o600);}catch(error){if((error as NodeJS.ErrnoException).code==='EEXIST')refuse('SSH key operation reserved; explicit recovery required');throw error;}
  try{return await operation();}
  finally {
    try{const owned=fstatSync(descriptor),current=lstatSync(lock);if(owned.dev!==current.dev||owned.ino!==current.ino)refuse('SSH key reservation changed; explicit recovery required');unlinkSync(lock);}
    finally{closeSync(descriptor);}
  }
}
function parts(publicKey:string):string[] {
  const text=publicKey.trim(),parts=text.split(/\s+/);
  if(text.includes('\n')||text.includes('\r')||parts.length<2||parts[0]!=='ssh-ed25519')refuse('SSH keypair is inconsistent');return parts.slice(0,2);
}
function fingerprint(publicKey:string):string {
  const encoded=parts(publicKey)[1];
  if(!/^[A-Za-z0-9+/]{68}$/.test(encoded))refuse('SSH keypair is inconsistent');
  const blob=Buffer.from(encoded,'base64');
  if(blob.length!==51||blob.readUInt32BE(0)!==11||blob.subarray(4,15).toString()!=='ssh-ed25519'||blob.readUInt32BE(15)!==32)refuse('SSH keypair is inconsistent');
  return 'SHA256:'+createHash('sha256').update(blob).digest('base64').replace(/=+$/,'');
}
function environmentCopy(environment:Env|null):Record<string,string>{return Object.fromEntries(Object.entries(environment??process.env).filter((entry):entry is [string,string]=>entry[1]!==undefined));}
async function verify(paths:string[],env:Record<string,string>,runner:BackendRunner,requirePair:boolean):Promise<Map> {
  const [directory,privatePath,publicPath]=paths;
  if(requirePair&&!(existsSync(privatePath)&&existsSync(publicPath)))refuse('owned SSH keypair is missing');
  chmodSync(directory,0o700);let derived:string|null=null,published:string|null=null;
  if(existsSync(privatePath)) {
    chmodSync(privatePath,0o600);const result=await runner(['ssh-keygen','-y','-P','','-f',privatePath],{cwd:directory,env,timeoutMs:30000});
    if(result.exit!==0||typeof result.out!=='string')refuse('SSH keypair is inconsistent');derived=result.out.trim();
  }
  if(existsSync(publicPath)){chmodSync(publicPath,0o600);published=readFileSync(publicPath,'utf8').trim();}
  if(derived&&published&&JSON.stringify(parts(derived))!==JSON.stringify(parts(published)))refuse('SSH keypair is inconsistent');
  const value=published||derived;if(!value)refuse('SSH keypair is inconsistent');
  return {public_key:value,fingerprint:fingerprint(value)};
}
async function acknowledge(callback:(...args:any[])=>unknown,...args:any[]):Promise<void>{try{if(await callback(...args)!==true)refuse('SSH key ownership update failed');}catch(error){if(cancelled(error))throw error;refuse('SSH key ownership update failed');}}
const planning=(opts:Map)=>opts['red/event']==='build'||opts['red/dry-run']===true;

export async function prepareKeypair(opts:Map,owned:unknown,environment:Env|null,recordIntent:()=>unknown,recordPrepared:(fingerprint:string)=>unknown,runner:BackendRunner=executeBackendCommand):Promise<Map> {
  try {
    const selected=mode(opts);if(selected.mode==='external')return selected;
    if(planning(opts)){const path='$HOME/.ssh/'+opts.profile;return {mode:'managed',private_key_path:path,public_key_path:path+'.pub',public_key:'ssh-ed25519 PLACEHOLDER managed-by-colors',fingerprint:null};}
    if(Object.hasOwn(opts,'red/event')&&opts['red/event']!=='create')refuse('SSH key preparation requires create');
    ownership(owned);const env=environmentCopy(environment),allPaths=paths(opts,env);safePaths(allPaths);
    const [directory,privatePath,publicPath]=allPaths;
    if(owned.status==='fresh'&&allPaths.slice(1).some(exists))refuse('unowned SSH key files exist; verify surviving hosts before recovery');
    if(owned.status==='prepared'&&!(existsSync(privatePath)&&existsSync(publicPath)))refuse('owned SSH keypair is missing');
    if(!existsSync(directory))mkdirSync(directory,{mode:0o700});chmodSync(directory,0o700);
    return await reservation(directory,opts.profile,async()=>{
      safePaths(allPaths);let verified:Map;
      if(owned.status==='fresh') {
        if(allPaths.slice(1).some(exists))refuse('unowned SSH key files exist; verify surviving hosts before recovery');
        await acknowledge(recordIntent);
        const result=await runner(['ssh-keygen','-q','-t','ed25519','-N','','-C',opts.profile+' managed by Colors','-f',privatePath],{cwd:directory,env,timeoutMs:30000});
        if(result.exit!==0)refuse('SSH key generation failed');
        verified=await verify(allPaths,env,runner,true);await acknowledge(recordPrepared,verified.fingerprint);
      }else{verified=await verify(allPaths,env,runner,true);if(verified.fingerprint!==owned.fingerprint)refuse('SSH key fingerprint differs from ownership');}
      return {mode:'managed',private_key_path:privatePath,public_key_path:publicPath,...verified};
    });
  }catch(error){if(error instanceof SSHError||cancelled(error))throw error;refuse('SSH key operation failed');}
}
export async function cleanupKeypair(opts:Map,owned:unknown,authority:unknown,environment:Env|null=null,runner:BackendRunner=executeBackendCommand):Promise<Map> {
  try {
    const selected=mode(opts);if(selected.mode==='external')return selected;
    if(planning(opts))return {mode:'managed',cleaned:false,planned:true};
    if(Object.hasOwn(opts,'red/event')&&opts['red/event']!=='delete')refuse('SSH key cleanup requires delete');
    if(!object(authority)||Object.keys(authority).some(k=>!['all_resources_destroyed','known_hosts_owned'].includes(k))||authority.all_resources_destroyed!==true||(Object.hasOwn(authority,'known_hosts_owned')&&typeof authority.known_hosts_owned!=='boolean'))refuse('SSH key cleanup requires complete resource destruction');
    ownership(owned);const env=environmentCopy(environment),allPaths=paths(opts,env);safePaths(allPaths);
    const [directory,privatePath,publicPath,known]=allPaths;if(!existsSync(directory))return {mode:'managed',cleaned:true};chmodSync(directory,0o700);
    return await reservation(directory,opts.profile,async()=>{
      safePaths(allPaths);
      if(existsSync(privatePath)||existsSync(publicPath)) {
        if(owned.status!=='prepared')refuse('unowned SSH key files exist; verify surviving hosts before recovery');
        const verified=await verify(allPaths,env,runner,false);if(verified.fingerprint!==owned.fingerprint)refuse('SSH key fingerprint differs from ownership');
        for(const path of [privatePath,publicPath])if(exists(path))unlinkSync(path);
      }
      if(authority.known_hosts_owned===true&&existsSync(known)){if(owned.status!=='prepared')refuse('SSH known-host ownership uncertain');unlinkSync(known);}
      return {mode:'managed',cleaned:true};
    });
  }catch(error){if(error instanceof SSHError||cancelled(error))throw error;refuse('SSH key operation failed');}
}
