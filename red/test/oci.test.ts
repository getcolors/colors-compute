import {test,expect} from 'bun:test';
import {backend_plan} from '../src/rendering.ts';
import {identity,journalPut} from '../src/journal.ts';
import {identityValid,coordination} from '../src/coordination.ts';
import {managedOciBackend} from '../src/managed-oci-backend.ts';
import type {BackendRunner} from '../src/backend.ts';
const opts={'oci-ocpus':1,'provider-backend':'oci','provider-compute':'oci','oci-bucket':'demo-states','oci-region':'eu-frankfurt-1','oci-namespace':'namespace1','oci-compartment-id':'ocid1.compartment.example',profile:'demo','oci-bucket-mode':'managed','compute-prevent-destroy':false};
test('OCI compatibility backend isolates credentials and has a native identity',()=>{
 const plan=backend_plan(opts,'demo/compute/shared.tfstate');expect(plan.config.terraform.backend.s3.endpoints.s3).toBe('https://namespace1.compat.objectstorage.eu-frankfurt-1.oraclecloud.com');expect(plan.credential_bindings).toEqual({COLORS_PAR_OCI_ACCESS_KEY_ID:'access_key',COLORS_PAR_OCI_SECRET_ACCESS_KEY:'secret_key'});expect(identityValid(identity(opts))).toBe(true);
});
test('native OCI journal uses If-Match and preserves 412',async()=>{
 const intent=coordination({status:'absent'},identity(opts),{type:'acquire',run_id:'run',write_id:'first',target_etag:null});intent.condition={if_match:'stale'};
 const runner:BackendRunner=async(args,options)=>{expect(args.slice(0,2)).toEqual(['oci','raw-request']);expect(JSON.parse(args[args.indexOf('--request-headers')+1]!)).toEqual({'if-match':'stale','content-type':'application/json'});expect(options.env.COLORS_PAR_OCI_SECRET_ACCESS_KEY).toBeUndefined();return {exit:0,out:JSON.stringify({status:'412 Precondition Failed'}),err:''};};
 expect(await journalPut(opts,intent,{},runner)).toEqual({status:'conflict'});
});
test('OCI absence requires a complete authorized compartment list',async()=>{
 const runner:BackendRunner=async()=>({exit:0,out:JSON.stringify({status:'404 Not Found'}),err:''});
 await expect(managedOciBackend(opts,'bootstrap',{},runner)).rejects.toThrow('absence unconfirmed');
});
test('OCI lifecycle retains ownership and removes all versions after retirement',async()=>{
 let bucket:any=null,marker:any=null,created=0;const removed:string[]=[];
 const runner:BackendRunner=async(args)=>{
  const method=args[args.indexOf('--http-method')+1],url=new URL(args[args.indexOf('--target-uri')+1]!),body=args.includes('--request-body')?JSON.parse(args[args.indexOf('--request-body')+1]!):undefined;
  const path=url.pathname,key=path.includes('/o/')?decodeURIComponent(path.split('/o/')[1]!):undefined;
  let data:any=null,status='200 OK';
  if(method==='DELETE'){removed.push(key?key+':'+url.searchParams.get('versionId'):'bucket');}
  else if(path.endsWith('/b')){if(method==='GET')data=[];else{created++;bucket=body;data=bucket;}}
  else if(path.endsWith('/objectversions'))data={items:[{name:'_colors/backend-owner.json',versionId:'3'},{name:'demo/shared.tfstate',versionId:'1'},{name:'demo/shared.tfstate',versionId:'2'}]};
  else if(path.endsWith('/o'))data={objects:[{name:'demo/shared.tfstate'}]};
  else if(key){if(method==='PUT')marker=body;else data=key==='_colors/backend-owner.json'?marker:{version:4,resources:[]};if(method==='GET'&&!data)status='404 Not Found';}
  else{if(method==='PUT')Object.assign(bucket,body);data=bucket;if(!data)status='404 Not Found';}
  return {exit:0,out:JSON.stringify({status,data,headers:{etag:'e1'}}),err:''};
 };
 expect(await managedOciBackend(opts,'bootstrap',{},runner)).toEqual({status:'ready',bucket:'demo-states'});
 bucket.versioning='Suspended';await managedOciBackend(opts,'bootstrap',{},runner);expect(created).toBe(1);expect(bucket.versioning).toBe('Enabled');
 let released=false;await expect(managedOciBackend(opts,'finalize',{},runner,()=>({acquire:async()=>{},snapshot:async()=>({document:{status:'active'}}),release:async()=>{released=true;}}))).rejects.toThrow('compute must retire');expect(released).toBe(true);expect(removed).toEqual([]);
 expect(await managedOciBackend(opts,'finalize',{},runner,()=>({acquire:async()=>{},snapshot:async()=>({document:{status:'retired'}}),release:async()=>{}}))).toEqual({status:'destroyed'});
 expect(removed).toEqual(['demo/shared.tfstate:1','demo/shared.tfstate:2','_colors/backend-owner.json:3','bucket']);expect(bucket.freeformTags['colors-phase']).toBe('deleting');
 await expect(managedOciBackend(opts,'bootstrap',{},runner)).rejects.toThrow('deletion in progress');
});
test('OCI recovery refuses provider resources including default boot-volume names',async()=>{
 const {recover_absent_oci_nodes}=await import('../src/recovery.ts');
 for(const resourceName of [null,'Boot volume of instance demo-0']){
  const transitions:any[]=[];let released=false;const domains:string[]=[];
  const owner={acquire:async()=>{},snapshot:async()=>({document:{status:'active',key:{phase:'prepared'},shared:{phase:'ready'},nodes:{'0':{phase:'failed',operation:'create',operation_id:'attempt',state_key:'demo/compute/nodes/0.tfstate'}}}}),transition:async(...args:any[])=>{transitions.push(args);},release:async()=>{released=true;}};
  const runner:BackendRunner=async(args)=>{const url=new URL(args[args.indexOf('--target-uri')+1]!);let data:any;
   if(url.pathname.includes('/o/'))data={version:4,serial:1,lineage:'line',outputs:{},resources:[]};
   else{const domain=url.searchParams.get('availabilityDomain');if(domain)domains.push(domain);data=domain==='ad3'&&resourceName?[{displayName:resourceName,lifecycleState:'AVAILABLE'}]:[];}
   return {exit:0,out:JSON.stringify({status:'200 OK',data,headers:{}}),err:''};};
  const run=()=>recover_absent_oci_nodes({...opts,'oci-availability-domain':'ad1','oci-availability-domains':['ad1','ad2','ad3']},{'0':'attempt'},{},runner,()=>owner);
  if(resourceName){await expect(run()).rejects.toThrow('absent OCI instances and boot volumes');expect(transitions).toEqual([]);}else{expect(await run()).toEqual({status:'recovered',nodes:['0']});expect(transitions).toEqual([['retry',{node_id:'0',evidence:'verified-provider-absence'}]]);}
  expect(domains).toEqual(['ad1','ad2','ad3']);expect(released).toBe(true);
 }
});
test('OCI versions follow header pages and deleting tag resumes an older active marker',async()=>{
 const marker={schema:1,identity:{bucket:'demo-states',region:'eu-frankfurt-1',namespace:'namespace1',compartment:'ocid1.compartment.example',profile:'demo'},status:'active'};const removed:string[]=[];
 const runner:BackendRunner=async(args)=>{const method=args[args.indexOf('--http-method')+1],url=new URL(args[args.indexOf('--target-uri')+1]!);let data:any={},headers:any={etag:'e1'};
  if(method==='DELETE')removed.push(url.searchParams.get('versionId')??'bucket');
  else if(url.pathname.endsWith('/objectversions')){expect(url.searchParams.has('start')).toBe(false);if(url.searchParams.has('page')){expect(url.searchParams.get('page')).toBe('next');data={items:[{name:'_colors/backend-owner.json',versionId:'2'}]};}else{data={items:[{name:'demo/old.tfstate',versionId:'1'}]};headers['opc-next-page']='next';}}
  else if(url.pathname.includes('/o/'))data=marker;
  else data={compartmentId:'ocid1.compartment.example',freeformTags:{'colors-profile':'demo','colors-purpose':'managed-backend','colors-phase':'deleting'}};
  return {exit:0,out:JSON.stringify({status:'200 OK',data,headers}),err:''};};
 expect(await managedOciBackend(opts,'finalize',{},runner,()=>{throw Error('journal was already purged');})).toEqual({status:'destroyed'});expect(removed).toEqual(['1','2','bucket']);
});
test('OCI domain placement validates all domains and preserves stable node indices',async()=>{
 const {placeNode}=await import('../src/oci.ts');const placed={...opts,'oci-availability-domain':'legacy','oci-availability-domains':['ad1','ad2','ad3']};
 expect(placeNode(placed,'node','broker-2')['oci-availability-domain']).toBe('ad3');expect(placeNode(placed,'node','3')['oci-availability-domain']).toBe('ad1');expect(placeNode(placed,'shared','shared')).toEqual(placed);expect(placed['oci-availability-domain']).toBe('legacy');
 for(const domains of [[],['ad1','ad1'],['${injected}'],'ad1'])expect(()=>placeNode({...placed,'oci-availability-domains':domains},'node','0')).toThrow('invalid OCI availability domains');
});
test('OCI recovery follows native inventory pages before accepting absence',async()=>{
 const {recover_absent_oci_nodes}=await import('../src/recovery.ts');const pages:(string|null)[]=[];
 const owner={acquire:async()=>{},snapshot:async()=>({document:{status:'active',key:{phase:'prepared'},shared:{phase:'ready'},nodes:{'0':{phase:'failed',operation:'create',operation_id:'attempt',state_key:'demo/compute/nodes/0.tfstate'}}}}),transition:async()=>{throw Error('must refuse');},release:async()=>{}};
 const runner:BackendRunner=async(args)=>{const url=new URL(args[args.indexOf('--target-uri')+1]!);if(url.pathname.includes('/o/'))return {exit:0,out:'{"status":"404 Not Found"}',err:''};const page=url.searchParams.get('page');pages.push(page);return {exit:0,out:JSON.stringify({status:'200 OK',data:page?[{displayName:'demo-0',lifecycleState:'RUNNING'}]:[],headers:page?{}:{'opc-next-page':'next'}}),err:''};};
 await expect(recover_absent_oci_nodes({...opts,'oci-availability-domain':'ad1'},{'0':'attempt'},{},runner,()=>owner)).rejects.toThrow('absent OCI instances and boot volumes');expect(pages).toEqual([null,'next']);
});
