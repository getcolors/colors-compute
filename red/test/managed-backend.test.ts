import {test,expect} from 'bun:test';
import {readFileSync,writeFileSync} from 'node:fs';
import {bootstrap_backend,finalize_backend,backend_presence} from '../src/managed-backend.ts';
const opts={'provider-backend':'s3','s3-bucket-mode':'managed','s3-bucket':'demo-state-123456789012-us-east-1','s3-region':'us-east-1',profile:'demo','compute-prevent-destroy':false};
function aws(exists=false){
 const calls:string[]=[];let marker:any={schema:1,identity:{account:'123456789012',bucket:opts['s3-bucket'],region:'us-east-1',profile:'demo'},status:'active'};
 let phase:any=null;const objects:any={};
 const runner=async(args:string[])=>{const op=args[2]!;calls.push(op);let value:any={};
  if(op==='get-caller-identity')value={Account:'123456789012'};
  if(op==='head-bucket'&&!exists)return {exit:1,out:'',err:'An error occurred (404) when calling the HeadBucket operation: missing'};
  if(op==='create-bucket')exists=true;
  if(op==='get-bucket-tagging')value={TagSet:[{Key:'colors:profile',Value:'demo'},{Key:'colors:owner',Value:'123456789012'},{Key:'colors:purpose',Value:'managed-backend'},...(phase?[{Key:'colors:phase',Value:phase}]:[])]};
  if(op==='put-bucket-tagging')phase=JSON.parse(args[args.indexOf('--tagging')+1]!).TagSet.find((x:any)=>x.Key==='colors:phase')?.Value;
  if(op==='get-object'){const key=args[args.indexOf('--key')+1]!;const document=key==='_colors/backend-owner.json'?marker:objects[key];if(document===null)return {exit:1,out:'',err:'An error occurred (NoSuchKey) when calling the GetObject operation: missing'};writeFileSync(args[args.indexOf('--key')+2]!,JSON.stringify(document));value={ETag:'"etag"'};}
  if(op==='put-object')marker=JSON.parse(readFileSync(args[args.indexOf('--body')+1]!,'utf8'));
  if(op==='list-objects-v2')value={Contents:Object.keys(objects).map(Key=>({Key}))};
  return {exit:0,out:JSON.stringify(value),err:''};
 };
 return {calls,runner,objects,setMarker:(m:any)=>marker=m,setPhase:(p:any)=>phase=p};
}
test('managed bootstrap creates once and configures protection',async()=>{const a=aws();await bootstrap_backend(opts,{},a.runner);await bootstrap_backend(opts,{},a.runner);expect(a.calls.filter(c=>c==='create-bucket').length).toBe(1);expect(a.calls).toContain('put-bucket-versioning');expect(a.calls).toContain('put-public-access-block');expect(a.calls).toContain('put-bucket-encryption');});
test('access denied cannot authorize bucket creation',async()=>{const calls:string[]=[];await expect(bootstrap_backend(opts,{},async args=>{calls.push(args[2]!);return args[2]==='get-caller-identity'?{exit:0,out:'{"Account":"123456789012"}',err:''}:{exit:1,out:'',err:'An error occurred (403) when calling the HeadBucket operation: denied'};})).rejects.toThrow();expect(calls).not.toContain('create-bucket');});
test('finalize refuses active Terraform state',async()=>{const a=aws(true);a.objects['demo/dns.tfstate']={version:4,resources:[{instances:[{}]}]};let released=false;const factory=()=>({acquire:async()=>{},snapshot:async()=>({document:{status:'retired'}}),release:async()=>{released=true;}});await expect(finalize_backend(opts,{},a.runner,factory)).rejects.toThrow();expect(a.calls).not.toContain('delete-bucket');expect(released).toBe(true);});
test('deleting tag permits retry after last marker version removed',async()=>{const a=aws(true);a.setMarker(null);a.setPhase('deleting');await expect(bootstrap_backend(opts,{},a.runner)).rejects.toThrow();expect(await finalize_backend(opts,{},a.runner)).toEqual({status:'destroyed'});});
test('external buckets perform no AWS calls',async()=>{const a=aws();expect(await bootstrap_backend({...opts,'s3-bucket-mode':'external'},{},a.runner)).toEqual({status:'skipped'});expect(a.calls).toEqual([]);});
test('presence is read-only and reports the managed bucket',async()=>{
 const missing=aws();expect(await backend_presence(opts,{},missing.runner)).toEqual({status:'absent'});expect(missing.calls).toEqual(['get-caller-identity','head-bucket']);
 expect(await backend_presence({...opts,'s3-bucket-mode':'external'},{},missing.runner)).toEqual({status:'skipped'});
 expect(await backend_presence({...opts,'red/dry-run':true},{},missing.runner)).toEqual({status:'skipped'});
 const existing=aws(true);expect(await backend_presence(opts,{},existing.runner)).toEqual({status:'present'});expect(existing.calls).toEqual(['get-caller-identity','head-bucket']);
});
