import {test,expect} from 'bun:test';
import {Runtime} from './orchestration.test.ts';
import {Runner,executionOpts,executionEnv,executionKey,executionDocuments,executionState,emptyState} from './execution.test.ts';
import cases from '../../test/fixtures/provider-requests.json';
import {check_deployment_drift} from '../src/drift.ts';import {checkState} from '../src/execution.ts';import {existsSync} from 'node:fs';
const [baseOpts,,base]:any=(cases as any[]).find(c=>c.args[0]['provider-compute']==='vultr'&&c.args[1]==='shared').args;
const requirements={security:base.security,network:base.network};
test('drift plans every owned state under journal lease, without changing lifecycle records',async()=>{
 const r=new Runtime();expect((await r.run()).status).toBe('ready');const opts={...baseOpts,profile:'demo','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'};
 Object.assign(r.states['demo/compute/shared.tfstate'],{ssh_key_id:'test-key'});Object.assign(r.states['demo/compute/shared.tfstate'].params,{vpc_id:'vpc',firewall_group_id:'fw'});
 const plans:string[]=[];const deps:any={journal_get:async()=>structuredClone(r.observed),journal_put:async(_o:any,intent:any)=>{if(intent.condition.if_match!==r.observed.etag)return {status:'conflict'};r.observed={status:'present',etag:'drift-'+intent.document.write_id,document:structuredClone(intent.document)};return {status:'written',etag:r.observed.etag};},read_state:async(_o:any,key:string)=>({status:'present',params:r.states[key].params,outputs:r.states[key]}),public_key:()=> 'ssh-ed25519 fixture',compute_credential_errors:()=>[],check_state:async(_o:any,key:string)=>{expect(r.observed.document.lock.state).toBe('held');plans.push(key);return {status:'clean'};}};
 const before=structuredClone(r.observed.document);expect(await check_deployment_drift(opts,[{count:2}],requirements,{},deps)).toEqual({status:'clean'});expect(plans).toEqual(['demo/compute/shared.tfstate','demo/compute/nodes/0.tfstate','demo/compute/nodes/1.tfstate']);
 for(const key of ['nodes','shared','key','generation','status','topology_declared'])expect(r.observed.document[key]).toEqual(before[key]);expect(r.observed.document.lock.state).toBe('idle');
 expect(await check_deployment_drift(opts,[{count:2}],{...requirements,private:true},{},deps)).toEqual({status:'error'});expect(plans).toHaveLength(3);
 deps.check_state=()=>({status:'error'});expect(await check_deployment_drift(opts,[{count:2}],requirements,{},deps)).toEqual({status:'error'});expect(r.observed.document.lock.state).toBe('idle');
 const revision=r.observed.document.revision;expect(await check_deployment_drift(opts,[{count:1}],requirements,{},deps)).toEqual({status:'error'});expect(r.observed.document.revision).toBe(revision);
});
for(const exit of [0,1,2,-1])test('drift executor plan exit '+exit+' never reaches apply',async()=>{
 const runner=new Runner(JSON.stringify(executionState));const result=await checkState(executionOpts,executionKey,executionDocuments,executionEnv,async(args,config)=>{const r=await runner.run(args,config);return args[1]==='plan'?{exit,out:'',err:'secret diagnostic'}:r;});
 expect(result).toEqual({status:exit===0?'clean':'error'});expect(runner.calls.map(c=>c[1])).toEqual(['init','state','plan']);expect(runner.calls.at(-1)).toContain('-detailed-exitcode');expect(runner.directories.every(d=>!existsSync(d))).toBe(true);
});
test('empty and absent states cannot pass drift',async()=>{for(const before of ['',JSON.stringify(emptyState)]){const runner=new Runner(before);expect(await checkState(executionOpts,executionKey,executionDocuments,executionEnv,runner.run)).toEqual({status:'error'});expect(runner.calls.some(c=>c[1]==='plan')).toBe(false);}});
test('managed drift key reads public file only without chmod or creation',async()=>{
 const {readDriftPublicKey}=await import('../src/drift.ts');const fs=await import('node:fs');const {tmpdir}=await import('node:os');const {join}=await import('node:path');const {createHash}=await import('node:crypto');
 const home=fs.mkdtempSync(join(tmpdir(),'drift-public-'));try{const dir=join(home,'.ssh');fs.mkdirSync(dir,0o755);const blob=Buffer.concat([Buffer.from('0000000b7373682d6564323535313900000020','hex'),Buffer.alloc(32)]),publicKey='ssh-ed25519 '+blob.toString('base64')+' comment',fingerprint='SHA256:'+createHash('sha256').update(blob).digest('base64').replace(/=+$/,'');const path=join(dir,'demo.pub');fs.writeFileSync(path,publicKey,{mode:0o644});expect(readDriftPublicKey({profile:'demo'},fingerprint,{HOME:home})).toBe(publicKey);expect(fs.existsSync(join(dir,'demo'))).toBe(false);expect(fs.statSync(path).mode&0o777).toBe(0o644);expect(fs.statSync(dir).mode&0o777).toBe(0o755);expect(()=>readDriftPublicKey({profile:'demo'},'SHA256:'+'A'.repeat(43),{HOME:home})).toThrow();fs.unlinkSync(path);fs.symlinkSync(join(home,'target'),path);expect(()=>readDriftPublicKey({profile:'demo'},fingerprint,{HOME:home})).toThrow();}finally{fs.rmSync(home,{recursive:true,force:true});}
});
