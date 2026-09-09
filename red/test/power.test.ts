import {test,expect} from 'bun:test';
import {Runtime} from './orchestration.test.ts';
import {power_deployment,provider_power} from '../src/power.ts';
const opts={profile:'demo','provider-compute':'vultr','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'},id='12345678-1234-1234-1234-123456789abc';
test('power reads immutable ID under lease, returns current IP, retains uncertain lease',async()=>{
 const r=new Runtime();expect((await r.run(1)).status).toBe('ready');r.states['demo/compute/nodes/0.tfstate'].params.provider_id=id;
 let calls=0;const deps:any={journal_get:()=>structuredClone(r.observed),journal_put:(_o:any,intent:any)=>{if(intent.condition.if_match!==r.observed.etag)return {status:'conflict'};r.observed={status:'present',etag:'power-'+intent.document.write_id,document:structuredClone(intent.document)};return {status:'written',etag:r.observed.etag};},read_state:(_o:any,key:string)=>({status:'present',params:r.states[key].params}),provider_power:(_o:any,action:string,identity:string)=>{calls++;expect(r.observed.document.lock.state).toBe('held');expect(action).toBe('start');expect(identity).toBe(id);return {status:'ready',ip:'203.0.113.20'};}};
 const result=await power_deployment(opts,'start',{},deps);expect(result.status).toBe('ready');expect(result.cluster.nodes[0].ip).toBe('203.0.113.20');expect(r.observed.document.lock.state).toBe('idle');expect(r.states['demo/compute/nodes/0.tfstate'].params.ip).toBe('192.0.2.1');
 deps.provider_power=()=>({status:'error'});expect(await power_deployment(opts,'stop',{},deps)).toEqual({status:'error'});expect(r.observed.document.lock.state).toBe('held');expect(await power_deployment(opts,'start',{},deps)).toEqual({status:'error'});expect(calls).toBe(1);
});
test('Vultr sends one mutation then polls, with no token in URL',async()=>{
 const calls:any[]=[];const states=['stopped','stopped','running'];const result=await provider_power(opts,'start',id,{'COLORS_PAR_VULTR_API_KEY':'synthetic-token'},{sleep:()=>{},http:(method:string,url:string,headers:any)=>{expect(headers.Authorization).toBe('Bearer synthetic-token');expect(url.includes('synthetic-token')).toBe(false);calls.push([method,url]);return method==='POST'?'':JSON.stringify({instance:{id,power_status:states.shift(),main_ip:'203.0.113.9'}});}});
 expect(result).toEqual({status:'ready',ip:'203.0.113.9'});expect(calls.map(c=>c[0])).toEqual(['GET','POST','GET','GET']);expect(calls[1][1].endsWith('/'+id+'/start')).toBe(true);
});
test('OCI uses explicit profile, fixed soft-stop arguments and sanitized exact env',async()=>{
 const identity='ocid1.instance.oc1.example',calls:string[][]=[];
 const result=await provider_power({'provider-compute':'oci','oci-config-file-profile':'OPERATOR'},'stop',identity,{HOME:'/temporary',TF_LOG:'TRACE',OCI_CLI_ENDPOINT:'https://untrusted.invalid',OCI_CLI_AUTH:'security_token'},{runner:(args:string[],cwd:string,env:any)=>{calls.push(args);expect(args.slice(0,5)).toEqual(['oci','--config-file','/temporary/.oci/config','--profile','OPERATOR']);expect(env.TF_LOG).toBeUndefined();expect(env.OCI_CLI_ENDPOINT).toBeUndefined();expect(env.OCI_CLI_AUTH).toBe('security_token');return {exit:0,out:JSON.stringify({data:{id:identity,'lifecycle-state':calls.length===1?'RUNNING':'STOPPED'}})};}});
 expect(result).toEqual({status:'ready'});expect(calls).toHaveLength(3);expect(calls[1]).toContain('SOFTSTOP');expect(calls[1]).toContain('--wait-for-state');
});
test('unsupported and planning are offline',async()=>{expect(await power_deployment({'provider-compute':'aws'},'start',{},{})).toEqual({status:'error'});expect(await power_deployment({...opts,'red/event':'build'},'start',{},{})).toEqual({status:'planned',action:'start'});});
