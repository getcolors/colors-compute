import {test,expect} from 'bun:test';
import {backend_plan} from '../src/rendering.ts';
import {identity} from '../src/journal.ts';
import {identityValid} from '../src/coordination.ts';
import {managedGcsBackend} from '../src/managed-gcs-backend.ts';
import {gcsClient,gcsPut} from '../src/gcs.ts';
const opts={'provider-backend':'gcs','provider-compute':'google','gcs-bucket':'demo-states','gcs-region':'us-central1','google-project':'demo-project',profile:'demo','gcs-bucket-mode':'managed','compute-prevent-destroy':false};
const runner=async()=>({exit:0,out:'test-token',err:''});
test('GCS uses a separate prefix per state and valid coordination identity',()=>{
 expect(backend_plan(opts,'demo/compute/shared.tfstate').config.terraform.backend).toEqual({gcs:{bucket:'demo-states',prefix:'demo/compute/shared.tfstate'}});
 expect(identityValid(identity(opts))).toBe(true);
});
test('generation writes use server preconditions and preserve conflicts',async()=>{
 const original=globalThis.fetch;globalThis.fetch=(async(url:any,init:any)=>{expect(new URL(url).searchParams.get('ifGenerationMatch')).toBe('123');expect(init.headers.Authorization).toBe('Bearer test-token');return new Response('',{status:412});}) as any;
 try{expect(await gcsPut(await gcsClient({},runner),'demo-states','journal',{},'123')).toEqual({conflict:true});}finally{globalThis.fetch=original;}
});
test('managed GCS refuses an unowned existing bucket',async()=>{
 const original=globalThis.fetch;globalThis.fetch=(async(input:any)=>Response.json(new URL(input).hostname==='cloudresourcemanager.googleapis.com'?{projectId:'demo-project',projectNumber:'123456789'}:{labels:{},location:'US-CENTRAL1',projectNumber:'123456789'})) as any;
 try{await expect(managedGcsBackend(opts,'bootstrap',{},runner)).rejects.toThrow('ownership mismatch');}finally{globalThis.fetch=original;}
});
test('managed GCS creates with protection and retains ownership on repeat bootstrap',async()=>{
 const original=globalThis.fetch;let bucket:any=null,marker:any=null,created=0;
 globalThis.fetch=(async(input:any,init:any)=>{const url=new URL(input);if(url.hostname==='cloudresourcemanager.googleapis.com')return Response.json({projectId:'demo-project',projectNumber:'123456789'});if(init.method==='PATCH'){Object.assign(bucket,JSON.parse(init.body));return Response.json(bucket);}if(url.pathname==='/storage/v1/b'&&init.method==='POST'){bucket={...JSON.parse(init.body),metageneration:'1',projectNumber:'123456789'};created++;return Response.json(bucket);}
 if(url.pathname==='/upload/storage/v1/b/demo-states/o'){marker=JSON.parse(init.body);return Response.json({generation:'1'});}
 if(url.pathname.includes('/o/'))return marker?Response.json(url.searchParams.get('alt')==='media'?marker:{generation:'1'}):new Response('',{status:404});
 return bucket?Response.json(bucket):new Response('',{status:404});}) as any;
 try{expect(await managedGcsBackend(opts,'bootstrap',{},runner)).toEqual({status:'ready',bucket:'demo-states'});bucket.versioning.enabled=false;bucket.iamConfiguration.publicAccessPrevention='inherited';expect(await managedGcsBackend(opts,'bootstrap',{},runner)).toEqual({status:'ready',bucket:'demo-states'});expect(created).toBe(1);expect(bucket.iamConfiguration.publicAccessPrevention).toBe('enforced');expect(bucket.versioning.enabled).toBe(true);}finally{globalThis.fetch=original;}
});
test('managed GCS refuses to delete before compute retirement',async()=>{
 const original=globalThis.fetch;const identity={project:'demo-project',bucket:'demo-states',region:'us-central1',profile:'demo'};let released=false;
 globalThis.fetch=(async(input:any)=>{const url=new URL(input);if(url.hostname==='cloudresourcemanager.googleapis.com')return Response.json({projectId:'demo-project',projectNumber:'123456789'});return Response.json(url.pathname.includes('/o/')?(url.searchParams.get('alt')==='media'?{schema:1,identity,status:'active'}:{generation:'1'}):{labels:{colors_profile:'demo',colors_project:'demo-project',colors_purpose:'managed-backend'},location:'US-CENTRAL1',metageneration:'1',projectNumber:'123456789'});}) as any;
 try{await expect(managedGcsBackend(opts,'finalize',{},runner,()=>({acquire:async()=>{},snapshot:async()=>({document:{status:'active'}}),release:async()=>{released=true;}}))).rejects.toThrow('compute must retire');expect(released).toBe(true);}finally{globalThis.fetch=original;}
});
test('managed GCS teardown deletes every generation after retirement',async()=>{
 const original=globalThis.fetch;const identity={project:'demo-project',bucket:'demo-states',region:'us-central1',profile:'demo'};let marker:any={schema:1,identity,status:'active'};const removed:string[]=[];
 globalThis.fetch=(async(input:any,init:any)=>{const url=new URL(input);if(url.hostname==='cloudresourcemanager.googleapis.com')return Response.json({projectId:'demo-project',projectNumber:'123456789'});const name=decodeURIComponent(url.pathname.split('/o/')[1]??'');
 if(init.method==='DELETE'){removed.push(name?`${name}:${url.searchParams.get('generation')}`:'bucket');return new Response(null,{status:204});}
 if(url.pathname.startsWith('/upload/')){marker=JSON.parse(init.body);return Response.json({generation:'3'});}
 if(init.method==='PATCH')return Response.json({});
 if(url.pathname.endsWith('/o'))return Response.json({items:url.searchParams.has('versions')?[{name:'demo/compute/shared.tfstate/default.tfstate',generation:'1'},{name:'demo/compute/shared.tfstate/default.tfstate',generation:'2'},{name:'_colors/backend-owner.json',generation:'3'}]:[{name:'demo/compute/shared.tfstate/default.tfstate'}]});
 if(name)return Response.json(url.searchParams.get('alt')==='media'?(name==='_colors/backend-owner.json'?marker:{version:4,resources:[]}):{generation:'1'});
 return Response.json({labels:{colors_profile:'demo',colors_project:'demo-project',colors_purpose:'managed-backend'},location:'US-CENTRAL1',metageneration:'1',projectNumber:'123456789'});}) as any;
 try{expect(await managedGcsBackend(opts,'finalize',{},runner,()=>({acquire:async()=>{},snapshot:async()=>({document:{status:'retired'}}),release:async()=>{}}))).toEqual({status:'destroyed'});expect(removed).toEqual(['demo/compute/shared.tfstate/default.tfstate:1','demo/compute/shared.tfstate/default.tfstate:2','_colors/backend-owner.json:3','bucket']);}finally{globalThis.fetch=original;}
});
test('GCS state presence uses the OpenTofu workspace object',async()=>{
 const {statePresence}=await import('../src/execution.ts');const original=globalThis.fetch;
 globalThis.fetch=(async(input:any)=>{expect(decodeURIComponent(new URL(input).pathname)).toEndWith('/o/demo/compute/shared.tfstate/default.tfstate');return Response.json({generation:'22'});}) as any;
 try{expect(await statePresence(opts,'demo/compute/shared.tfstate',{},runner)).toEqual({status:'present'});}finally{globalThis.fetch=original;}
});
test('bounded GCS journal reads reject large generations before downloading',async()=>{
 const {gcsGet}=await import('../src/gcs.ts');let calls=0;
 await expect(gcsGet(async()=>{calls++;return {generation:'1',size:'2097153'};},'states','journal',2097152)).rejects.toThrow('too large');
 expect(calls).toBe(1);
 await expect(gcsGet(async(_method,_path,_body,query)=>query?.alt?[]:{generation:'1',size:'2'},'states','journal',2097152)).rejects.toThrow('invalid GCS document');
});

test('matching managed labels and marker cannot authorize another project',async()=>{
 const original=globalThis.fetch;
 try{for(const action of ['bootstrap','finalize'])for(const projectNumber of ['987654321',undefined,123456789]){
  const methods:string[]=[];
  globalThis.fetch=(async(input:any,init:any)=>{methods.push(init.method);return Response.json(new URL(input).hostname==='cloudresourcemanager.googleapis.com'?{projectId:'demo-project',projectNumber:'123456789'}:{projectNumber,location:'US-CENTRAL1',labels:{colors_profile:'demo',colors_project:'demo-project',colors_purpose:'managed-backend'}});}) as any;
  await expect(managedGcsBackend(opts,action,{},runner)).rejects.toThrow('ownership mismatch');
  expect(methods).toEqual(['GET','GET']);
 }}finally{globalThis.fetch=original;}
});
test('failed or malformed project resolution prevents bucket access',async()=>{
 const original=globalThis.fetch;
 try{for(const response of [new Response('',{status:403}),new Response('',{status:404}),Response.json({}),Response.json({projectId:'foreign-project',projectNumber:'123456789'}),Response.json({projectId:'demo-project',projectNumber:123456789}),Response.json({projectId:'demo-project',projectNumber:'0'}),Response.json({projectId:'demo-project',projectNumber:'1.2'})]){
  let calls=0;
  globalThis.fetch=(async(input:any,init:any)=>{calls++;expect(String(input)).toBe('https://cloudresourcemanager.googleapis.com/v1/projects/demo-project');expect(init.headers.Authorization).toBe('Bearer test-token');return response;}) as any;
  await expect(managedGcsBackend(opts,'bootstrap',{},runner)).rejects.toThrow();expect(calls).toBe(1);
 }}finally{globalThis.fetch=original;}
});
test('object 404 means absent only when bucket remains readable',async()=>{
 const original=globalThis.fetch;
 try{for(const status of [200,404,403]){
  globalThis.fetch=(async(input:any)=>new URL(input).pathname.includes('/o/')?new Response('',{status:404}):status===200?Response.json({name:'demo-states'}):new Response('',{status})) as any;
  const request=await gcsClient({},runner);
  if(status===200)expect(await request('GET','storage/v1/b/demo-states/o/missing')).toBeNull();
  else await expect(request('GET','storage/v1/b/demo-states/o/missing')).rejects.toThrow(status===404?'bucket missing':'403');
  if(status===404)expect(await request('GET','storage/v1/b/demo-states')).toBeNull();
 }}finally{globalThis.fetch=original;}
});
test('object 404 refuses malformed bucket metadata',async()=>{
 const original=globalThis.fetch;
 try{for(const bucket of [{},{conflict:true},{name:'foreign'},[]]){
  globalThis.fetch=(async(input:any)=>new URL(input).pathname.includes('/o/')?new Response('',{status:404}):Response.json(bucket)) as any;
  await expect((await gcsClient({},runner))('GET','storage/v1/b/demo-states/o/missing')).rejects.toThrow('invalid GCS bucket metadata');
 }}finally{globalThis.fetch=original;}
});
test('created bucket project must match before marker write',async()=>{
 const original=globalThis.fetch;const methods:string[]=[];
 globalThis.fetch=(async(input:any,init:any)=>{const url=new URL(input);methods.push(`${init.method} ${url.pathname}`);
  if(url.hostname==='cloudresourcemanager.googleapis.com')return Response.json({projectId:'demo-project',projectNumber:'123456789'});
  if(init.method==='GET')return new Response('',{status:404});
  return Response.json({...JSON.parse(init.body),projectNumber:'987654321'});
 }) as any;
 try{await expect(managedGcsBackend(opts,'bootstrap',{},runner)).rejects.toThrow('ownership mismatch');expect(methods).toEqual(['GET /v1/projects/demo-project','GET /storage/v1/b/demo-states','POST /storage/v1/b']);}finally{globalThis.fetch=original;}
});
