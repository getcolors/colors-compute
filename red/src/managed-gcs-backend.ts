import {gcsClient,gcsGet,gcsPut,bucketPath,objectPath} from './gcs.ts';
import {Coordinator} from './coordinator.ts';
import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;
const MARKER='_colors/backend-owner.json';
export async function managedGcsBackend(opts:Map,action:string,environment:Map=process.env,runner:BackendRunner=executeBackendCommand,coordinatorFactory?:any){
 const mode=opts['gcs-bucket-mode']??'external';if(!['external','managed'].includes(mode))throw Error('invalid GCS bucket mode');if(mode==='external')return {status:'skipped'};
 if(['blue','red','green'].some(c=>opts[`${c}/dry-run`]===true||opts[`${c}/event`]==='build'))return {status:'skipped'};
 const bucket=opts['gcs-bucket'],region=opts['gcs-region'],project=opts['google-project'],profile=opts.profile;
 if(!/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(bucket??'')||!/^[a-z][a-z0-9-]+$/.test(region??'')||!/^[a-z][a-z0-9-]+$/.test(project??'')||!/^[a-z0-9][a-z0-9_-]{0,62}$/.test(profile??''))throw Error('invalid managed GCS identity');
 const request=await gcsClient(environment,runner),path=bucketPath(bucket),identity={project,bucket,region,profile};
 const resolved=await request('GET',`v1/projects/${project}`);
 if(resolved?.projectId!==project||typeof resolved.projectNumber!=='string'||!/^[1-9][0-9]*$/.test(resolved.projectNumber))throw Error('managed backend project verification failed');
 const projectNumber=resolved.projectNumber;
 const labels={colors_profile:profile,colors_project:project,colors_purpose:'managed-backend'};
 let metadata=await request('GET',path),owner:any,deleting=false;
 try{
  if(action==='presence')return {status:metadata===null?'absent':'present'};
  if(metadata===null){
   if(action==='finalize'||['blue','red','green'].some(c=>opts[`${c}/event`]==='delete'))return {status:'absent'};
   if(opts['compute-require-existing-state']===true)throw Error('existing managed backend required');
   metadata=await request('POST','storage/v1/b',{name:bucket,location:region,labels,iamConfiguration:{uniformBucketLevelAccess:{enabled:true},publicAccessPrevention:'enforced'},versioning:{enabled:true},softDeletePolicy:{retentionDurationSeconds:'0'}},{project});
   if(metadata?.projectNumber!==projectNumber)throw Error('managed backend ownership mismatch');
   const written=await gcsPut(request,bucket,MARKER,{schema:1,identity,status:'active'},'0');if(written?.conflict)throw Error('managed backend ownership conflict');
  }
  if(metadata.projectNumber!==projectNumber||Object.entries(labels).some(([k,v])=>metadata.labels?.[k]!==v)||metadata.location?.toLowerCase()!==region)throw Error('managed backend ownership mismatch');
  const observed=await gcsGet(request,bucket,MARKER);let marker=observed?.document;
  if(!marker&&action==='finalize'&&metadata.labels.colors_phase==='deleting')marker={schema:1,identity,status:'deleting'};
  if(!marker||marker.schema!==1||!marker.identity||Object.keys(marker.identity).length!==4||Object.entries(identity).some(([k,v])=>marker.identity[k]!==v)||!['active','deleting'].includes(marker.status))throw Error('managed backend ownership mismatch');
  if(action==='bootstrap'){
   if(marker.status!=='active'||metadata.labels.colors_phase==='deleting')throw Error('managed backend deletion in progress');
   const protectedBucket=await request('PATCH',path,{iamConfiguration:{uniformBucketLevelAccess:{enabled:true},publicAccessPrevention:'enforced'},versioning:{enabled:true},softDeletePolicy:{retentionDurationSeconds:'0'}},{ifMetagenerationMatch:metadata.metageneration});
   if(protectedBucket?.conflict)throw Error('managed backend metadata conflict');
   return {status:'ready',bucket};
  }
  if(opts['compute-prevent-destroy']!==false)throw Error('managed backend deletion protected');
  const list=async(versions=false)=>{let pageToken:string|undefined;const items:Map[]=[];do{const page=await request('GET',`${path}/o`,undefined,{...(versions?{versions:true}:{}),...(pageToken?{pageToken}:{})});items.push(...(page.items??[]));pageToken=page.nextPageToken;}while(pageToken);return items;};
  if(marker.status==='active'){
   owner=coordinatorFactory?coordinatorFactory(opts,{environment,eventPrefix:'lifecycle/'}):new Coordinator(opts,{environment,eventPrefix:'lifecycle/'});
   await owner.acquire();if((await owner.snapshot()).document.status!=='retired')throw Error('compute must retire before backend deletion');
   for(const item of await list()){
    if(item.name===MARKER||item.name===`${profile}/compute/coordination.json`)continue;
    if(!item.name.startsWith(`${profile}/`)||!item.name.endsWith('.tfstate/default.tfstate'))throw Error('managed backend contains unexpected objects');
    const state=(await gcsGet(request,bucket,item.name))?.document;
    if(state?.version!==4||!Array.isArray(state.resources)||state.resources.some((r:Map)=>r.instances?.length))throw Error('managed backend contains live state');
   }
   const result=await gcsPut(request,bucket,MARKER,{...marker,status:'deleting'},observed!.etag);if(result?.conflict)throw Error('managed backend ownership conflict');deleting=true;
  }
  const tagged=await request('PATCH',path,{labels:{...metadata.labels,colors_phase:'deleting'}},{ifMetagenerationMatch:metadata.metageneration});if(tagged?.conflict)throw Error('managed backend metadata conflict');
  const versions=await list(true);versions.sort((a,b)=>Number(a.name===MARKER)-Number(b.name===MARKER));
  for(const item of versions){const r=await request('DELETE',objectPath(bucket,item.name),undefined,{generation:item.generation,ifGenerationMatch:item.generation});if(r?.conflict)throw Error('managed backend generation conflict');}
  await request('DELETE',path);return {status:'destroyed'};
 }finally{if(owner&&!deleting)await owner.release();}
}
