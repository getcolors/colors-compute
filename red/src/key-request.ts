import {lstatSync,openSync,readSync,closeSync,constants} from 'node:fs';
import {homedir} from 'node:os';
import {join,extname} from 'node:path';
import {createHash} from 'node:crypto';
import providers from '../resources/providers.json';
import recipes from '../resources/provider-recipes.json';
type Map=Record<string,any>;
const missing=(v:any)=>v==null||(typeof v==='string'&&(!v.trim()||v.trim().toUpperCase()==='REPLACE_ME'));
function publicKey(value:any):string {
 const error=()=>{throw Error('invalid external SSH public key');};
 if(typeof value!=='string')return error();value=value.trim();const parts=value.split(/\s+/);
 if(parts.length<2||/[\r\n]/.test(value)||!['ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256','ecdsa-sha2-nistp384','ecdsa-sha2-nistp521'].includes(parts[0]))return error();
 if(!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(parts[1]))return error();
 const blob=Buffer.from(parts[1],'base64');if(blob.length<4)return error();const n=blob.readUInt32BE(0);
 if(blob.length<=4+n||!blob.subarray(4,4+n).equals(Buffer.from(parts[0])))return error();return value;
}
export function key_request(opts:Map,prepared:Map,environment:Record<string,string|undefined>|null=null) {
 if(prepared.mode==='managed')return {mode:'managed',public_key:prepared.public_key};
 if(prepared.mode!=='external')throw Error('invalid compute key request');
 const provider=opts['provider-compute'];if(typeof provider!=='string'||!Object.hasOwn(recipes,provider))throw Error('compute provider recipe unavailable');
 const kind=(providers.compute as Map)[provider]['ssh-aliases']?.[prepared.setting]??(recipes as Map)[provider].external_key_kind;
 const refs=Array.isArray(prepared.reference)?prepared.reference:[prepared.reference];
 if(kind==='ids') {
  if(!refs.length||refs.some(v=>!(typeof v==='string'&&!missing(v)||typeof v==='number'&&Number.isSafeInteger(v)&&v>0)))throw Error('invalid external SSH key reference');
  return {mode:'external',ids:structuredClone(refs),reference:refs[0]};
 }
 if(refs.length!==1||typeof refs[0]!=='string'||missing(refs[0]))throw Error('one external SSH public key is required');
 if(kind==='fingerprint_file'&&(!refs[0].endsWith('.pub')||/[\0\r\n]/.test(refs[0])))throw Error('external SSH key must name a regular .pub file');
 if(opts['red/event']==='build'||opts['red/dry-run']===true){if(kind==='fingerprint_file'){const value=Array(16).fill('00').join(':');return {mode:'external',ids:[value],reference:value};}return {mode:'external',public_key:'ssh-ed25519 PLACEHOLDER managed-by-colors'};}
 let value:string;
 if(kind==='content')value=publicKey(refs[0]);
 else if(kind==='public_file'||kind==='fingerprint_file') {
  const env=environment??process.env;const path=refs[0].startsWith('~/')?join(env.HOME||homedir(),refs[0].slice(2)):refs[0];
  let valid=false;try{valid=extname(path)==='.pub'&&lstatSync(path).isFile();}catch{}
  if(!valid)throw Error('external SSH key must name a regular .pub file');
  let fd:number|undefined;
  try{fd=openSync(path,constants.O_RDONLY|constants.O_NOFOLLOW);const bytes=Buffer.alloc(262149);let count=0,n=0;while(count<bytes.length&&(n=readSync(fd,bytes,count,bytes.length-count,null))>0)count+=n;
   const content=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes.subarray(0,count));if([...content].length>65536)throw Error();value=publicKey(content);
  }catch{throw Error('invalid external SSH public key file');}finally{if(fd!==undefined)closeSync(fd);}
 }else throw Error('unsupported external SSH key reference');
 if(kind==='fingerprint_file'){const fingerprint=createHash('md5').update(Buffer.from(value.split(/\s+/)[1],'base64')).digest('hex').match(/../g)!.join(':');return {mode:'external',ids:[fingerprint],reference:fingerprint};}
 return {mode:'external',public_key:value};
}
