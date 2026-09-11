import {ociClient,bucketPath,objectPath} from './oci.ts';
import {Coordinator} from './coordinator.ts';
import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;
const MARKER='_colors/backend-owner.json';
function check(value:unknown,message:string):asserts value {if(!value)throw Error(message);}
export async function managedOciBackend(opts:Map,action:string,environment:Map=process.env,runner:BackendRunner=executeBackendCommand,coordinatorFactory?:any){
 const mode=opts['oci-bucket-mode']??'external';check(['external','managed'].includes(mode),'invalid OCI bucket mode');
 if(mode==='external'||['blue','red','green'].some(c=>opts[`${c}/dry-run`]===true||opts[`${c}/event`]==='build'))return {status:'skipped'};
 const bucket=opts['oci-bucket'],region=opts['oci-region'],namespace=opts['oci-namespace'],compartment=opts['oci-compartment-id'],profile=opts.profile;
 check(/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(bucket??'')&&/^[a-z][a-z0-9-]+$/.test(region??'')&&/^[a-zA-Z0-9]+$/.test(namespace??'')&&typeof compartment==='string'&&compartment.startsWith('ocid1.')&&/^[a-z0-9][a-z0-9_-]{0,62}$/.test(profile??''),'invalid managed OCI identity');
 const request=await ociClient(opts,environment,runner),path=bucketPath(opts),collection=path.slice(0,path.lastIndexOf('/')),identity={bucket,region,namespace,compartment,profile},tags={'colors-profile':profile,'colors-purpose':'managed-backend'};
 let observed=await request('GET',path),owner:any,deleting=false;
 try{
  if(observed===null){
   let page:string|undefined;do{const listed=await request('GET',collection,undefined,{compartmentId:compartment,...(page?{page}:{})});check(listed&&Array.isArray(listed.data)&&!listed.data.some((b:Map)=>b.name===bucket),'OCI bucket absence unconfirmed');page=listed.headers['opc-next-page'];}while(page);
   if(action==='finalize'||['blue','red','green'].some(c=>opts[`${c}/event`]==='delete'))return {status:'absent'};
   check(opts['compute-require-existing-state']!==true,'existing managed backend required');
   observed=await request('POST',collection,{name:bucket,compartmentId:compartment,freeformTags:tags,publicAccessType:'NoPublicAccess',versioning:'Enabled'});
   check(observed&&!observed.conflict,'managed backend creation conflict');
   const written=await request('PUT',objectPath(opts,MARKER),{schema:1,identity,status:'active'},{},{'if-none-match':'*','content-type':'application/json'});check(written&&!written.conflict,'managed backend ownership conflict');
  }
  const metadata=observed.data;check(metadata?.compartmentId===compartment&&Object.entries(tags).every(([k,v])=>metadata.freeformTags?.[k]===v),'managed backend ownership mismatch');
  const markerResponse=await request('GET',objectPath(opts,MARKER));let marker=markerResponse?.data;
  if(!marker&&action==='finalize'&&metadata.freeformTags['colors-phase']==='deleting')marker={schema:1,identity,status:'deleting'};
  check(marker?.schema===1&&marker.identity&&Object.keys(marker.identity).length===5&&Object.entries(identity).every(([k,v])=>marker.identity[k]===v)&&['active','deleting'].includes(marker.status),'managed backend ownership mismatch');
  if(action==='finalize'&&metadata.freeformTags['colors-phase']==='deleting')marker={...marker,status:'deleting'};
  const update=async(body:Map)=>{const result=await request('PUT',path,body,{},{'if-match':observed!.headers.etag});check(result&&!result.conflict,'managed backend metadata conflict');};
  if(action==='bootstrap'){
   check(marker.status==='active'&&metadata.freeformTags['colors-phase']!=='deleting','managed backend deletion in progress');
   if(metadata.versioning!=='Enabled'||metadata.publicAccessType!=='NoPublicAccess')await update({versioning:'Enabled',publicAccessType:'NoPublicAccess'});
   return {status:'ready',bucket};
  }
  check(opts['compute-prevent-destroy']===false,'managed backend deletion protected');
  const list=async(versions=false)=>{const items:Map[]=[];let start:string|undefined;do{const response=await request('GET',path+(versions?'/objectversions':'/o'),undefined,start?{[versions?'page':'start']:start}:{});check(response&&!response.conflict,'managed backend listing failed');check(Array.isArray(response.data?.[versions?'items':'objects']),'managed backend listing malformed');items.push(...response.data[versions?'items':'objects']);start=versions?response.headers['opc-next-page']:response.data.nextStartWith;}while(start);return items;};
  if(marker.status==='active'){
   owner=coordinatorFactory?coordinatorFactory(opts,{environment,eventPrefix:'lifecycle/'}):new Coordinator(opts,{environment,eventPrefix:'lifecycle/'});
   await owner.acquire();check((await owner.snapshot()).document.status==='retired','compute must retire before backend deletion');
   for(const item of await list()){
    if(item.name===MARKER||item.name===`${profile}/compute/coordination.json`)continue;
    check(item.name.startsWith(`${profile}/`)&&item.name.endsWith('.tfstate'),'managed backend contains unexpected objects');
    const state=(await request('GET',objectPath(opts,item.name)))?.data;check(state?.version===4&&Array.isArray(state.resources)&&!state.resources.some((r:Map)=>r.instances?.length),'managed backend contains live state');
   }
   const written=await request('PUT',objectPath(opts,MARKER),{...marker,status:'deleting'},{},{'if-match':markerResponse!.headers.etag,'content-type':'application/json'});check(written&&!written.conflict,'managed backend ownership conflict');deleting=true;
  }
  await update({freeformTags:{...metadata.freeformTags,'colors-phase':'deleting'}});
  const versions=await list(true);versions.sort((a,b)=>Number(a.name===MARKER)-Number(b.name===MARKER));
  for(const item of versions){check(item.versionId,'managed backend version missing');const deleted=await request('DELETE',objectPath(opts,item.name),undefined,{versionId:item.versionId});check(!deleted?.conflict,'managed backend version conflict');}
  const deleted=await request('DELETE',path);check(!deleted?.conflict,'managed backend bucket deletion conflict');return {status:'destroyed'};
 }finally{if(owner&&!deleting)await owner.release();}
}
