/** Redacted, bounded diagnostics. Never includes command output or state contents. */
import {accessSync,constants,statSync} from 'node:fs';
import {delimiter,isAbsolute,resolve} from 'node:path';
type Map=Record<string,any>;
export const messages:Record<string,string>={command_failed:'Required command failed.',missing_credentials:'Required credentials are not set.',state_unreadable:'Compute state could not be read.',state_absent:'Required compute state is absent.',identity_mismatch:'Compute state identity does not match the requested node.',unsafe_plan:'Compute plan requires an unauthorized change.',invalid_request:'Invalid compute request.',key_access_failed:'SSH key access could not be prepared.',filesystem_error:'Compute working files could not be accessed.',internal_error:'Compute operation failed.'};
export function redact(stderr:unknown,opts:Map,environment:Map):string|undefined{
 if(typeof stderr!=='string'||!stderr.trim())return undefined;
 let text=stderr.replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\)/g,'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'').replace(/[\p{Cc}\p{Cf}]/gu,char=>char==='\n'||char==='\t'?char:'');
 // Remove blocks first: replacing a known header must never strand a key payload.
 if(/(?:^|\n)\s*[\[{]/.test(text)||/[{]\s*(?:["':]|[A-Za-z][\w-]*\s*:)|\[\s*["'{]/.test(text)||/"(?:resources|planned_values|resource_changes|outputs|private_key_openssh)"\s*:/.test(text))return '[structured output suppressed]';
 text=text.replace(/-----BEGIN [^-\r\n]+-----[\s\S]*?(?:-----END [^-\r\n]+-----|$)/g,'[private material suppressed]');
 const secrets:string[]=[];
 function collect(v:any,key='',inherited=false){const sensitive=inherited||/(?:secret|token|password|credential|authorization|access.?key|api.?key|private.?key)/i.test(key)||key.startsWith('COLORS_PAR_');if(typeof v==='string'&&v&&sensitive)secrets.push(v);else if(v&&typeof v==='object')for(const [k,x] of Object.entries(v))collect(x,k,sensitive);}
 collect(environment);collect(opts);
 for(const secret of secrets.sort((a,b)=>b.length-a.length)){const encoded=JSON.stringify(secret).slice(1,-1);let uri='';try{uri=encodeURIComponent(secret);}catch{}for(const value of new Set([secret,encoded,uri,uri.replaceAll('%20','+'),Buffer.from(secret).toString('base64')]))if(value)text=text.split(value).join('[REDACTED]');}
 text=text.replace(/(authorization\s*[:=]\s*)[^\r\n]*/gi,'$1[REDACTED]').replace(/\bBearer\s+[^\s"'<>]+/gi,'Bearer [REDACTED]');
 text=text.replace(/((?:[A-Za-z0-9_-]*(?:secret|token|password|access[_-]?key|api[_-]?key|private[_-]?key)[A-Za-z0-9_-]*)\s*[:=]\s*)[^\r\n]*/gi,'$1[REDACTED]');
 return text.slice(0,2000);
}
export function executable(program:string,cwd:string,environment:Map):string|undefined{
 const candidates=program.includes('/')?[isAbsolute(program)?program:resolve(cwd,program)]:typeof environment.PATH==='string'?environment.PATH.split(delimiter).map((p:string)=>resolve(cwd,p||'.',program)):[];
 for(const path of candidates)try{if(statSync(path).isFile()){accessSync(path,constants.X_OK);return path;}}catch{}return undefined;
}
export class CommandFailure extends Error {constructor(public details:Map){super('command failed');}}
export function commandFailure(args:string[],cwd:string,env:Map,result:Map,opts:Map,source:Map){const command=args[0]==='tofu'?args.slice(0,args[1]==='state'?3:2):args[0]==='aws'?args.slice(0,3):args[0]==='ssh-keygen'?['ssh-keygen']:args[0]==='gcloud'?args.slice(0,3):[args[0]];const resolved=executable(args[0],cwd,env),stderr=redact(result.err,opts,source);return new CommandFailure({command,...(resolved&&redact(resolved,opts,source)===resolved&&!/[\p{Cc}\p{Cf}]/u.test(resolved)?{executable:resolved}:{}),...(Number.isInteger(result.exit)?{exit_code:result.exit}:{}),...(stderr?{stderr}:{})});}
export function failure(error:unknown,stage:string,infrastructure_changes:string){
 const message=error instanceof Error?error.message:'';let code=error instanceof CommandFailure?(stage==='state'?'state_unreadable':stage==='access'?'key_access_failed':'command_failed'):stage==='credentials'?'missing_credentials':stage==='state'?'state_unreadable':stage==='plan-validation'?'unsafe_plan':stage==='access'?'key_access_failed':['validate','build'].includes(stage)?'invalid_request':'internal_error';
 if(!(error instanceof CommandFailure)&&stage!=='validate'){if(/identity|provider mismatch|provider cannot change|backend cannot change/.test(message))code='identity_mismatch';else if(/absent|state required|state is required|required state/.test(message))code='state_absent';else if(/credential/.test(message))code='missing_credentials';else if(/unsafe.*(?:file|directory)|compute file|OpenTofu file|OpenTofu directory/.test(message)||(error as Map)?.code&&/^(?:EACCES|EPERM|ENOENT|ELOOP|EISDIR|ENOTDIR|EMFILE|ENOSPC)$/.test((error as Map).code))code='filesystem_error';}
 return {status:'error',error:{code,stage,message:messages[code],infrastructure_changes,...(error instanceof CommandFailure?error.details:{})}};
}
