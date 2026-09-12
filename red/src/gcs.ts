import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;
/** GCS JSON API. Generation preconditions make journal writes atomic. */
export async function gcsClient(environment:Map=process.env,runner:BackendRunner=executeBackendCommand){
 const env=Object.fromEntries(Object.entries(environment).filter(([k,v])=>typeof v==='string'&&!/^(COLORS_PAR_|TF_|TOFU_)/.test(k))) as Record<string,string>;
 const token=await runner(['gcloud','auth','print-access-token','--quiet'],{cwd:process.cwd(),env,timeoutMs:120000});
 if(token.exit||!token.out.trim())throw Error('GCS authentication failed');
 const request=async(method:string,path:string,body?:unknown,query:Map={}):Promise<any>=>{
  const url=new URL((path.startsWith('v1/projects/')?'https://cloudresourcemanager.googleapis.com/':'https://storage.googleapis.com/')+path);for(const [k,v] of Object.entries(query))url.searchParams.set(k,String(v));
  const response=await fetch(url,{method,headers:{Authorization:`Bearer ${token.out.trim()}`,...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(120000)});
  if(response.status===404){
   if(method==='GET'&&path.startsWith('storage/v1/b/')&&path.includes('/o/')){
    const parent=path.split('/o/')[0],bucket=await request('GET',parent);
    if(bucket===null)throw Error('GCS bucket missing');
    if(typeof bucket?.name!=='string'||bucketPath(bucket.name)!==parent)throw Error('invalid GCS bucket metadata');
   }
   return null;
  }
  if(response.status===412)return {conflict:true};
  if(!response.ok)throw Error(`GCS operation failed (${response.status})`);
  const text=await response.text();return text?JSON.parse(text):{};
 };
 return request;
}
export const bucketPath=(bucket:string)=>`storage/v1/b/${encodeURIComponent(bucket)}`;
export const objectPath=(bucket:string,key:string)=>`${bucketPath(bucket)}/o/${encodeURIComponent(key)}`;
export async function gcsGet(request:Awaited<ReturnType<typeof gcsClient>>,bucket:string,key:string,maxBytes=Infinity){
 const metadata=await request('GET',objectPath(bucket,key));if(metadata===null)return null;
 if(Number.isFinite(maxBytes)&&(!/^\d+$/.test(metadata.size??'')||Number(metadata.size)>maxBytes))throw Error('GCS object too large or missing size');
 const document=await request('GET',objectPath(bucket,key),undefined,{alt:'media',generation:metadata.generation});
 if(document===null)throw Error('GCS generation disappeared');
 if(Number.isFinite(maxBytes)&&(typeof document!=='object'||Array.isArray(document)||Buffer.byteLength(JSON.stringify(document),'utf8')>maxBytes))throw Error('invalid GCS document');return {document,etag:metadata.generation};
}
export async function gcsPut(request:Awaited<ReturnType<typeof gcsClient>>,bucket:string,key:string,document:Map,generation:string){
 return request('POST',`upload/storage/v1/b/${encodeURIComponent(bucket)}/o`,document,{uploadType:'media',name:key,ifGenerationMatch:generation});
}
