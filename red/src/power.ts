import {request} from 'node:https';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir,homedir} from 'node:os';
import {join} from 'node:path';
import {isIP} from 'node:net';
import descriptors from '../resources/power-providers.json';
import {copy} from './copy.ts';
import {collect,registry,state_keys} from './index.ts';
import {Coordinator} from './coordinator.ts';
import {journalGet,journalPut} from './journal.ts';
import {readState,executeBackendCommand} from './backend.ts';
import {lifecycleDocumentValid} from './lifecycle.ts';
import {mode} from './ssh.ts';
type Map=Record<string,any>;
const require=(v:any)=>{if(!v)throw Error('compute power refused');};
const parse=(value:any)=>{require(typeof value==='string'&&Buffer.byteLength(value)<=2097152);return JSON.parse(value);};
function safeOutput(value:any,env:Map){const serialized=JSON.stringify(value),names=new Set(['compute','backend'].flatMap(section=>Object.values((registry as Map)[section]).flatMap((entry:any)=>(entry.secrets??[]).map((key:string)=>'COLORS_PAR_'+key.toUpperCase().replaceAll('-','_')))));require(!Object.entries(env).some(([k,v])=>names.has(k)&&typeof v==='string'&&v&&(serialized.includes(v)||serialized.includes(JSON.stringify(v).slice(1,-1)))));}
function nativeHttp(method:string,url:string,headers:Map):Promise<string>{return new Promise((resolve,reject)=>{let timer:ReturnType<typeof setTimeout>;const finish=(value:string)=>{clearTimeout(timer);resolve(value);},fail=(error:any)=>{clearTimeout(timer);reject(error);};const req=request(url,{method,headers,timeout:30000},res=>{if(!res.statusCode||res.statusCode<200||res.statusCode>=300){res.destroy();fail(Error('compute power refused'));return;}const parts:Buffer[]=[];let size=0;res.on('data',part=>{size+=part.length;if(size>2097152){res.destroy();fail(Error('compute power refused'));}else parts.push(part);});res.on('error',fail);res.on('end',()=>{try{finish(new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(Buffer.concat(parts)));}catch(e){fail(e);}});});req.on('timeout',()=>req.destroy(Error('compute power timeout')));req.on('error',fail);timer=setTimeout(()=>req.destroy(Error('compute power timeout')),30000);req.end();});}
async function internalProviderPower(opts:Map,action:string,providerId:string,environment:Map,dependencies:Map={}):Promise<Map>{
 const env={...environment},d=(descriptors as Map)[opts['provider-compute']];require(d&&Object.hasOwn(d.actions,action));require(typeof providerId==='string'&&new RegExp('^(?:'+d.id_pattern+')$').exec(providerId)?.[0]===providerId);
 const wait=Object.hasOwn(opts,'power-wait-seconds')?opts['power-wait-seconds']:300;require(Number.isInteger(wait)&&wait>=1&&wait<=1800);const target=d.actions[action];
 const call=async(name:string,f:any,...args:any[])=>await(dependencies[name]??f)(...args);let result:Map={status:'ready'};
 if(d.transport==='oci'){
  const profile=opts['oci-config-file-profile'];require(typeof profile==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.exec(profile)?.[0]===profile);
  const childEnv=Object.fromEntries(Object.entries(env).filter(([k])=>!k.startsWith('TF_')&&!k.startsWith('TOFU_')&&!k.startsWith('OCI_CLI_')||k==='OCI_CLI_AUTH'));
  const prefix=['oci','--config-file',join(env.HOME||homedir(),'.oci/config'),'--profile',profile,'--cli-rc-file','/dev/null','--no-retry','--output','json'];
  const command=async(args:string[],timeout:number)=>{safeOutput(prefix.concat(args),env);const cwd=mkdtempSync(join(tmpdir(),'colors-power-'));try{const r=await call('runner',(argv:string[],directory:string,environment:Map,timeoutMs:number)=>executeBackendCommand(argv,{cwd:directory,env:environment,timeoutMs}),prefix.concat(args),cwd,childEnv,timeout);require(r.exit===0);return parse(r.out);}finally{rmSync(cwd,{recursive:true,force:true});}};
  const args=['compute','instance','get','--instance-id',providerId];let instance=(await command(args,30000)).data??{};
  require(instance.id===providerId&&typeof instance['lifecycle-state']==='string');
  if(instance['lifecycle-state']!==target.state){await command(['compute','instance','action','--instance-id',providerId,'--action',target.action,'--wait-for-state',target.state,'--max-wait-seconds',String(wait)],wait*1000+30000);instance=(await command(args,30000)).data??{};}
  require(instance.id===providerId&&instance['lifecycle-state']===target.state);
  if(action==='start'){const vnics=(await command(['compute','instance','list-vnics','--instance-id',providerId],30000)).data;require(Array.isArray(vnics));const addresses=vnics.filter((v:any)=>v&&typeof v==='object'&&v['public-ip']).map((v:any)=>v['public-ip']);require(addresses.length===1&&typeof addresses[0]==='string'&&isIP(addresses[0])===4);result.ip=addresses[0];}
 }else{
  const token=env[d.credential];require(typeof token==='string'&&token.trim()&&token.trim().toUpperCase()!=='REPLACE_ME'&&!/[\r\n]/.test(token));
  const headers={Authorization:'Bearer '+token,Accept:'application/json'},url=d.origin+'/instances/'+providerId;
  const http=async(method:string,endpoint:string)=>{const value=await call('http',nativeHttp,method,endpoint,{...headers});return method==='GET'?parse(value):null;};
  const current=async()=>{const instance=(await http('GET',url)).instance??{};require(instance.id===providerId&&typeof instance.power_status==='string');return instance;};
  let instance=await current();if(instance.power_status!==target.state){await http('POST',url+'/'+target.action);const deadline=performance.now()+wait*1000;for(let i=0;i<Math.floor(wait/5)+2;i++){instance=await current();if(instance.power_status===target.state)break;require(performance.now()<deadline);await call('sleep',(seconds:number)=>new Promise(r=>setTimeout(r,seconds*1000)),Math.min(5,Math.max(0,(deadline-performance.now())/1000)));}}
  require(instance.power_status===target.state);if(action==='start'){require(typeof instance.main_ip==='string'&&isIP(instance.main_ip)===4);result.ip=instance.main_ip;}
 }
 safeOutput(result,env);return result;
}
export async function provider_power(opts:Map,action:string,providerId:string,environment:Map,dependencies:Map={}):Promise<Map>{try{return await internalProviderPower(opts,action,providerId,environment,dependencies);}catch(error){if(error instanceof Error&&error.name==='AbortError')throw error;throw Error('compute power refused');}}
export async function power_deployment(input:Map,action:string,environment:Map=process.env,dependencies:Map={}):Promise<Map>{
 const opts=copy(input),env={...environment};let owner:Coordinator|undefined,acquired=false,dispatched=false;
 const call=async(name:string,f:any,...args:any[])=>await(dependencies[name]??f)(...args);
 const existing=async()=>{const observed=await call('journal_get',journalGet,opts,env);require(observed.status==='present');return observed;};
 const valid=(doc:Map)=>{require(lifecycleDocumentValid(doc)&&doc.status==='active'&&doc.topology_declared&&doc.shared.phase==='ready'&&doc.key.phase==='prepared');const active=Object.entries(doc.nodes).filter(([,n]:any)=>n.phase!=='destroyed') as [string,Map][];require(active.length===1&&active[0][1].desired&&active[0][1].phase==='ready');require(Object.values(doc.nodes).every((n:any)=>n.phase!=='destroyed'||!n.desired));return active[0];};
 try{
  const d=(descriptors as Map)[opts['provider-compute']];require(d&&Object.hasOwn(d.actions,action));if(opts['red/event']==='build'||opts['red/dry-run']===true)return {status:'planned',action};
  const observed=await existing();valid(observed.document);require(observed.document.lock.state==='idle');
  owner=new Coordinator(opts,{environment:env,read:existing,write:(intent)=>call('journal_put',journalPut,opts,intent,env),eventPrefix:'lifecycle/'});await owner.acquire();acquired=true;
  const doc=(await owner.snapshot())!.document,[nodeId,node]=valid(doc),selected=mode(opts);require(selected.mode===doc.key.mode);
  const shared=await call('read_state',readState,opts,state_keys(opts.profile,[]).shared,env);require(shared.status==='present'&&shared.params?.provider===opts['provider-compute']);
  const state=await call('read_state',readState,opts,node.state_key,env);require(state.status==='present');
  const declarations=[{node_id:nodeId,role:node.role,index:node.index,provider:opts['provider-compute']}],cluster=collect(declarations,[state.params??{}],nodeId),providerId=cluster.nodes[0].provider_id;
  require(typeof providerId==='string'&&new RegExp('^(?:'+d.id_pattern+')$').exec(providerId)?.[0]===providerId);safeOutput(providerId,env);dispatched=true;
  const powered=await call('provider_power',provider_power,opts,action,providerId,env);require(powered.status==='ready');if(action==='start'){require(typeof powered.ip==='string'&&isIP(powered.ip)===4);cluster.nodes[0].ip=powered.ip;}
  const key:Map={mode:selected.mode},identity=selected.mode==='managed'?join(env.HOME||homedir(),'.ssh',opts.profile):selected.private_key_path;if(identity){key.private_key_path=identity;cluster.nodes[0].ssh_identity_file=identity;}
  const result={status:'ready',action,cluster,key};safeOutput(result,env);await owner.release();acquired=false;return result;
 }catch(error){if(error instanceof Error&&error.name==='AbortError')throw error;if(acquired&&!dispatched)try{await owner!.release();}catch{}return {status:'error'};}
}

export {nativeHttp as nativePowerHttp};
