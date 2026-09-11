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
 const original=globalThis.fetch;globalThis.fetch=(async()=>Response.json({labels:{},location:'US-CENTRAL1'})) as any;
 try{await expect(managedGcsBackend(opts,'bootstrap',{},runner)).rejects.toThrow('ownership mismatch');}finally{globalThis.fetch=original;}
});
test('managed GCS creates with protection and retains ownership on repeat bootstrap',async()=>{
 const original=globalThis.fetch;let bucket:any=null,marker:any=null,created=0;
 globalThis.fetch=(async(input:any,init:any)=>{const url=new URL(input);if(url.pathname==='/storage/v1/b'&&init.method==='POST'){bucket={...JSON.parse(init.body),metageneration:'1'};created++;return Response.json(bucket);}
 if(url.pathname==='/upload/storage/v1/b/demo-states/o'){marker=JSON.parse(init.body);return Response.json({generation:'1'});}
 if(url.pathname.includes('/o/'))return marker?Response.json(url.searchParams.get('alt')==='media'?marker:{generation:'1'}):new Response('',{status:404});
 return bucket?Response.json(bucket):new Response('',{status:404});}) as any;
 try{expect(await managedGcsBackend(opts,'bootstrap',{},runner)).toEqual({status:'ready',bucket:'demo-states'});expect(await managedGcsBackend(opts,'bootstrap',{},runner)).toEqual({status:'ready',bucket:'demo-states'});expect(created).toBe(1);expect(bucket.iamConfiguration.publicAccessPrevention).toBe('enforced');expect(bucket.versioning.enabled).toBe(true);}finally{globalThis.fetch=original;}
});
test('managed GCS refuses to delete before compute retirement',async()=>{
 const original=globalThis.fetch;const identity={project:'demo-project',bucket:'demo-states',region:'us-central1',profile:'demo'};let released=false;
 globalThis.fetch=(async(input:any)=>{const url=new URL(input);return Response.json(url.pathname.includes('/o/')?(url.searchParams.get('alt')==='media'?{schema:1,identity,status:'active'}:{generation:'1'}):{labels:{colors_profile:'demo',colors_project:'demo-project',colors_purpose:'managed-backend'},location:'US-CENTRAL1',metageneration:'1'});}) as any;
 try{await expect(managedGcsBackend(opts,'finalize',{},runner,()=>({acquire:async()=>{},snapshot:async()=>({document:{status:'active'}}),release:async()=>{released=true;}}))).rejects.toThrow('compute must retire');expect(released).toBe(true);}finally{globalThis.fetch=original;}
});
test('managed GCS teardown deletes every generation after retirement',async()=>{
 const original=globalThis.fetch;const identity={project:'demo-project',bucket:'demo-states',region:'us-central1',profile:'demo'};let marker:any={schema:1,identity,status:'active'};const removed:string[]=[];
 globalThis.fetch=(async(input:any,init:any)=>{const url=new URL(input),name=decodeURIComponent(url.pathname.split('/o/')[1]??'');
 if(init.method==='DELETE'){removed.push(name?`${name}:${url.searchParams.get('generation')}`:'bucket');return new Response(null,{status:204});}
 if(url.pathname.startsWith('/upload/')){marker=JSON.parse(init.body);return Response.json({generation:'3'});}
 if(init.method==='PATCH')return Response.json({});
 if(url.pathname.endsWith('/o'))return Response.json({items:url.searchParams.has('versions')?[{name:'demo/compute/shared.tfstate/default.tfstate',generation:'1'},{name:'demo/compute/shared.tfstate/default.tfstate',generation:'2'},{name:'_colors/backend-owner.json',generation:'3'}]:[{name:'demo/compute/shared.tfstate/default.tfstate'}]});
 if(name)return Response.json(url.searchParams.get('alt')==='media'?(name==='_colors/backend-owner.json'?marker:{version:4,resources:[]}):{generation:'1'});
 return Response.json({labels:{colors_profile:'demo',colors_project:'demo-project',colors_purpose:'managed-backend'},location:'US-CENTRAL1',metageneration:'1'});}) as any;
 try{expect(await managedGcsBackend(opts,'finalize',{},runner,()=>({acquire:async()=>{},snapshot:async()=>({document:{status:'retired'}}),release:async()=>{}}))).toEqual({status:'destroyed'});expect(removed).toEqual(['demo/compute/shared.tfstate/default.tfstate:1','demo/compute/shared.tfstate/default.tfstate:2','_colors/backend-owner.json:3','bucket']);}finally{globalThis.fetch=original;}
});
test('GCS state presence uses the OpenTofu workspace object',async()=>{
 const {statePresence}=await import('../src/execution.ts');const original=globalThis.fetch;
 globalThis.fetch=(async(input:any)=>{expect(decodeURIComponent(new URL(input).pathname)).toEndWith('/o/demo/compute/shared.tfstate/default.tfstate');return Response.json({generation:'22'});}) as any;
 try{expect(await statePresence(opts,'demo/compute/shared.tfstate',{},runner)).toEqual({status:'present'});}finally{globalThis.fetch=original;}
});
