import {managedOciBackend} from './managed-oci-backend.ts';
import {managedGcsBackend} from './managed-gcs-backend.ts';
/** Owned S3 bootstrap; disposal only after package states and compute retire. */
import {mkdtempSync,chmodSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {executeBackendCommand,type BackendRunner} from './backend.ts';
import {Coordinator} from './coordinator.ts';
type Map=Record<string,any>;
const MARKER='_colors/backend-owner.json';
async function lifecycle(opts:Map,action:string,environment:Map=process.env,runner:BackendRunner=executeBackendCommand,coordinatorFactory?:any){
 if(opts['provider-backend']==='oci')return managedOciBackend(opts,action,environment,runner,coordinatorFactory);
 if(opts['provider-backend']==='gcs')return managedGcsBackend(opts,action,environment,runner,coordinatorFactory);
 const mode=opts['s3-bucket-mode']??'external';
 if(!['external','managed'].includes(mode))throw Error('invalid S3 bucket mode');
 if(mode==='external')return {status:'skipped'};
 if(opts['provider-backend']!=='s3')throw Error('managed backend requires S3');
 if(['blue','red','green'].some(c=>opts[`${c}/dry-run`]===true||opts[`${c}/event`]==='build'))return {status:'skipped'};
 const bucket=opts['s3-bucket'],region=opts['s3-region'],profile=opts.profile;
 if(typeof bucket!=='string'||! /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(bucket)||typeof region!=='string'||! /^[a-z]{2}(?:-[a-z]+)+-\d$/.test(region)||typeof profile!=='string'||! /^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/.test(profile))throw Error('invalid managed backend identity');
 const env:Record<string,string>=Object.fromEntries(Object.entries(environment).filter(([k,v])=>typeof v==='string'&&!/^(COLORS_PAR_|TF_|TOFU_)/.test(k))) as Record<string,string>;
 Object.assign(env,{AWS_PAGER:'',AWS_CLI_AUTO_PROMPT:'off',AWS_MAX_ATTEMPTS:'1'});
 const directory=mkdtempSync(join(tmpdir(),'colors-backend-'));chmodSync(directory,0o700);
 let owner:any,deleting=false;
 try{
  const command=async(service:string,operation:string,args:string[]=[],missing:string[]=[])=>{
   const r=await runner(['aws',service,operation,...args,'--region',region,'--output','json','--no-cli-pager'],{cwd:directory,env,timeoutMs:120000});
   if(r.exit){const match=/An error occurred \(([^()]+)\) when calling the /.exec(r.err);if(match&&missing.includes(match[1]!))return null;throw Error('managed backend AWS operation failed');}
   return r.out.trim()?JSON.parse(r.out):{};
  };
  const account=(await command('sts','get-caller-identity')).Account;if(typeof account!=='string'||! /^\d{12}$/.test(account))throw Error('invalid AWS account');
  const identity={account,bucket,region,profile};
  const s3=(operation:string,args:string[]=[],missing:string[]=[])=>command('s3api',operation,['--bucket',bucket,'--expected-bucket-owner',account,...args],missing);
  const get=async(key:string)=>{const path=join(directory,'read.json');const r=await s3('get-object',['--key',key,path],key===MARKER?['NoSuchKey']:[]);if(r===null)return {document:null,etag:null};return {document:JSON.parse(readFileSync(path,'utf8')),etag:r.ETag};};
  const put=async(document:Map,etag?:string)=>{const path=join(directory,'write.json');writeFileSync(path,JSON.stringify(document),{mode:0o600});return s3('put-object',['--key',MARKER,'--body',path,'--content-type','application/json',...(etag?['--if-match',etag]:['--if-none-match','*'])]);};
  const present=await s3('head-bucket',[],['404','NoSuchBucket','NotFound']);
  if(action==='presence')return {status:present===null?'absent':'present'};
  if(present===null){
   if(action==='finalize'||['blue','red','green'].some(c=>opts[`${c}/event`]==='delete'))return {status:'absent'};
   if(opts['compute-require-existing-state']===true)throw Error('existing managed backend required');
   await command('s3api','create-bucket',['--bucket',bucket,'--object-ownership','BucketOwnerEnforced',...(region==='us-east-1'?[]:['--create-bucket-configuration',JSON.stringify({LocationConstraint:region})])]);
   await s3('put-bucket-tagging',['--tagging',JSON.stringify({TagSet:[{Key:'colors:profile',Value:profile},{Key:'colors:owner',Value:account},{Key:'colors:purpose',Value:'managed-backend'}]})]);
   await put({schema:1,identity,status:'active'});
  }
  const tags=Object.fromEntries((await s3('get-bucket-tagging')).TagSet.map((t:Map)=>[t.Key,t.Value]));
  if(tags['colors:profile']!==profile||tags['colors:owner']!==account||tags['colors:purpose']!=='managed-backend')throw Error('managed backend ownership mismatch');
  if(((await s3('get-bucket-location')).LocationConstraint||'us-east-1')!==region)throw Error('managed backend region mismatch');
  const observed=await get(MARKER);let marker=observed.document;const etag=observed.etag;
  if(marker===null&&action==='finalize'&&tags['colors:phase']==='deleting')marker={schema:1,identity,status:'deleting'};
  if(!marker||marker.schema!==1||!marker.identity||Object.keys(marker.identity).length!==4||Object.entries(identity).some(([k,v])=>marker.identity[k]!==v)||!['active','deleting'].includes(marker.status))throw Error('managed backend ownership mismatch');
  if(action==='bootstrap'){
   if(marker.status!=='active'||tags['colors:phase']==='deleting')throw Error('managed backend deletion in progress');
   await s3('put-public-access-block',['--public-access-block-configuration',JSON.stringify({BlockPublicAcls:true,IgnorePublicAcls:true,BlockPublicPolicy:true,RestrictPublicBuckets:true})]);
   await s3('put-bucket-encryption',['--server-side-encryption-configuration',JSON.stringify({Rules:[{ApplyServerSideEncryptionByDefault:{SSEAlgorithm:'AES256'}}]})]);
   await s3('put-bucket-versioning',['--versioning-configuration','{"Status":"Enabled"}']);
   return {status:'ready',bucket};
  }
  if(opts['compute-prevent-destroy']!==false)throw Error('managed backend deletion protected');
  if(marker.status==='active'){
   owner=coordinatorFactory?coordinatorFactory(opts,{environment:env,eventPrefix:'lifecycle/'}):new Coordinator(opts,{environment:env,eventPrefix:'lifecycle/'});
   await owner.acquire();const doc=(await owner.snapshot()).document;if(doc.status!=='retired')throw Error('compute must retire before backend deletion');
   for(const item of (await s3('list-objects-v2')).Contents??[]){const key=item.Key;
    if(key===MARKER||key===profile+'/compute/coordination.json')continue;
    if(!key.startsWith(profile+'/')||!key.endsWith('.tfstate'))throw Error('managed backend contains unexpected objects');
    const {document:state}=await get(key);if(state.version!==4||!Array.isArray(state.resources)||state.resources.some((r:Map)=>r.instances?.length))throw Error('managed backend contains live state');
   }
   await put({...marker,status:'deleting'},etag);deleting=true;
  }
  await s3('put-bucket-tagging',['--tagging',JSON.stringify({TagSet:Object.entries({...tags,'colors:phase':'deleting'}).map(([Key,Value])=>({Key,Value}))})]);
  while(true){
   const versions=await s3('list-object-versions');const entries=[...(versions.Versions??[]),...(versions.DeleteMarkers??[])];
   const others=entries.filter(x=>x.Key!==MARKER);const batch=(others.length?others:entries).slice(0,1000);
   if(!batch.length)break;
   const r=await s3('delete-objects',['--delete',JSON.stringify({Objects:batch.map(x=>({Key:x.Key,VersionId:x.VersionId})),Quiet:true})]);if(r.Errors?.length)throw Error('managed backend version deletion failed');
  }
  await s3('delete-bucket');return {status:'destroyed'};
 }finally{try{if(owner&&!deleting)await owner.release();}finally{rmSync(directory,{recursive:true,force:true});}}
}
export const bootstrap_backend=(opts:Map,environment:Map=process.env,runner:BackendRunner=executeBackendCommand)=>lifecycle(opts,'bootstrap',environment,runner);
/** Read-only: does the managed bucket exist? skipped for external mode, build and dry-run. */
export const backend_presence=(opts:Map,environment:Map=process.env,runner:BackendRunner=executeBackendCommand)=>lifecycle(opts,'presence',environment,runner);
export const finalize_backend=(opts:Map,environment:Map=process.env,runner:BackendRunner=executeBackendCommand,coordinatorFactory?:any)=>lifecycle(opts,'finalize',environment,runner,coordinatorFactory);
