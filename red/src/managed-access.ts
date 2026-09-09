import {constants,openSync,closeSync,writeSync,fsyncSync,renameSync,unlinkSync,lstatSync,mkdirSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {registry} from './index.ts';
import {parseStateEnvelope} from './backend.ts';
type Map=Record<string,any>;
const obj=(v:any):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const safe=(v:any)=>typeof v==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/.exec(v)?.[0]===v;
const demand=(v:any,message='invalid managed outputs')=>{if(!v)throw Error(message);};
export function managed_kubeconfig_path(opts:Map):string {demand(typeof opts.workdir==='string'&&!!opts.workdir.trim()&&safe(opts.profile),'invalid managed access path');return resolve(opts.workdir,opts.profile,'kubeconfig');}
export function publicParams(value:any,provider:string):Map {
 const required=['provider','kind','name','cluster_id','endpoint'];demand(obj(value)&&required.every(k=>Object.hasOwn(value,k))&&Object.keys(value).every(k=>[...required,'pod_cidr','service_cidr'].includes(k)));
 demand(value.provider===provider&&value.kind==='managed-kubernetes'&&safe(value.name));demand(typeof value.cluster_id==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.exec(value.cluster_id)?.[0]===value.cluster_id);
 demand(typeof value.endpoint==='string');const url=new URL(value.endpoint);demand(url.protocol==='https:'&&url.hostname&&!url.username&&!url.password&&!url.hash&&!url.search);
 for(const key of ['pod_cidr','service_cidr'])if(Object.hasOwn(value,key)){const v=value[key];demand(typeof v==='string');const m=/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(v);demand(m&&m[0]===v);const a=m!.slice(1).map(Number);demand(a.slice(0,4).every(n=>n>=0&&n<=255)&&a[4]!<=32&&a.map(String).join('.')===v.replace('/','.'));const n=((a[0]!<<24)|(a[1]!<<16)|(a[2]!<<8)|a[3]!)>>>0;demand((a[4]===0?n===0:(n&((2**(32-a[4]!)-1)>>>0))===0));}
 return {...value};
}
export class AccessDecoder {
 private content:Uint8Array|null=null;
 private secrets:string[];
 constructor(private opts:Map,environment:Record<string,string|undefined>=process.env){const keys=[...(registry.compute as Map)[opts["provider-compute"]].secrets,...(opts["provider-backend"]==="r2"?["r2-access-key-id","r2-secret-access-key"]:[])];this.secrets=keys.map(k=>environment["COLORS_PAR_"+k.toUpperCase().replaceAll("-","_")]).filter((v):v is string=>typeof v==="string"&&!!v);}
 decode=(text:string):Map=>{
  const {document,params}=parseStateEnvelope(text);const publicValue=publicParams(params,this.opts['provider-compute']),outputs=document.outputs;demand(Object.keys(outputs).length===2&&Object.hasOwn(outputs,'params')&&Object.hasOwn(outputs,'kubeconfig_b64')&&(outputs.params.sensitive??false)===false);const entry=outputs.kubeconfig_b64;demand(obj(entry)&&entry.sensitive===true&&typeof entry.value==='string'&&entry.value.length<=2796204);
  const raw=Buffer.from(entry.value,'base64');demand(raw.toString('base64')===entry.value&&raw.length>0&&raw.length<=2097152);const content=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(raw);demand(!content.includes('\0')&&!content.startsWith('\ufeff'));
  demand(!this.secrets.some(secret=>content.includes(secret)||content.includes(JSON.stringify(secret).slice(1,-1))));const config=Bun.YAML.parse(content) as Map;demand(obj(config)&&config.kind==='Config'&&config.apiVersion==='v1');demand(Array.isArray(config.clusters)&&config.clusters.length===1&&obj(config.clusters[0])&&obj(config.clusters[0].cluster));const cluster=config.clusters[0].cluster;demand(typeof cluster.server==='string'&&cluster.server.replace(/\/+$/,'')===params.endpoint.replace(/\/+$/,'')&&(cluster['insecure-skip-tls-verify']??false)===false);const contexts=config.contexts,users=config.users;demand(Array.isArray(contexts)&&contexts.length===1&&Array.isArray(users)&&users.length===1);const context=contexts[0],user=users[0];demand(obj(context)&&obj(user)&&obj(context.context)&&obj(user.user));const names=[config.clusters[0].name,context.name,user.name];demand(names.every(name=>typeof name==='string'&&!!name.trim()));demand(config['current-context']===names[1]&&context.context.cluster===names[0]&&context.context.user===names[2]);const credentials=user.user;demand((typeof credentials.token==='string'&&!!credentials.token.trim())||['client-certificate-data','client-key-data'].every(key=>typeof credentials[key]==='string'&&!!credentials[key].trim()));const forbidden=new Set(['exec','auth-provider','tokenFile','client-certificate','client-key','certificate-authority','proxy-url','tls-server-name']),seen=new Set<object>(),pending:any[]=[config];
  while(pending.length){const v=pending.pop();if(v&&typeof v==='object'){demand(!seen.has(v)&&seen.size<10000);seen.add(v);if(!Array.isArray(v))demand(Object.keys(v).every(k=>!forbidden.has(k)));pending.push(...Object.values(v));}}
  this.content=raw;return {params:publicValue};
 };
 write():string {
  demand(this.content!==null,'managed access unavailable');const target=managed_kubeconfig_path(this.opts);let part=target;
  for(;;){try{const stat=lstatSync(part);demand(!stat.isSymbolicLink(),'invalid managed access path');if(part===target)demand(stat.isFile()&&stat.uid===process.getuid!(),'invalid managed access path');}catch(error){if((error as any).code!=='ENOENT')throw error;}const parent=dirname(part);if(parent===part)break;part=parent;}
  mkdirSync(dirname(target),{mode:0o700,recursive:true});const temporary=resolve(dirname(target),`.kubeconfig-${randomUUID()}`);let fd:number|undefined;
  try{fd=openSync(temporary,constants.O_WRONLY|constants.O_CREAT|constants.O_EXCL|constants.O_NOFOLLOW,0o600);let offset=0;while(offset<this.content!.length){const count=writeSync(fd,this.content!,offset,this.content!.length-offset);demand(count>0);offset+=count;}fsyncSync(fd);closeSync(fd);fd=undefined;renameSync(temporary,target);return target;}finally{if(fd!==undefined)closeSync(fd);try{unlinkSync(temporary);}catch{}this.content=null;}
 }
}
