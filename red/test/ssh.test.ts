import {expect,test} from 'bun:test';
import {mkdtempSync,rmSync,existsSync,statSync,writeFileSync,unlinkSync,symlinkSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {prepareKeypair,cleanupKeypair} from '../src/ssh.ts';
const opts={profile:'demo','provider-compute':'vultr'};
const fresh={status:'fresh'};
const temporary=()=>mkdtempSync(join(tmpdir(),'colors-ssh-test-'));
const env=(home:string)=>({HOME:home,PATH:'/usr/bin:/bin'});
const prepared=(value:any)=>({status:'prepared',fingerprint:value.fingerprint});
test('actual managed generation verifies pair and callbacks before returning ownership',async()=>{
  const home=temporary();const calls:string[]=[];
  try {
    const key=await prepareKeypair(opts,fresh,env(home),()=>{expect(existsSync(join(home,'.ssh','demo'))).toBe(false);calls.push('intent');return true;},fingerprint=>{expect(fingerprint).toMatch(/^SHA256:[A-Za-z0-9+/]{43}$/);calls.push('prepared');return true;});
    expect(calls).toEqual(['intent','prepared']);expect(statSync(join(home,'.ssh')).mode&0o777).toBe(0o700);expect(statSync(key.private_key_path).mode&0o777).toBe(0o600);expect(statSync(key.public_key_path).mode&0o777).toBe(0o600);expect(JSON.stringify(key)).not.toContain('PRIVATE KEY');
    const reused=await prepareKeypair(opts,prepared(key),env(home),()=>{throw new Error('must not call');},()=>{throw new Error('must not call');});expect(reused).toEqual(key);
    expect(await cleanupKeypair(opts,prepared(key),{all_resources_destroyed:true},env(home))).toEqual({mode:'managed',cleaned:true});expect(existsSync(key.private_key_path)).toBe(false);expect(existsSync(key.public_key_path)).toBe(false);expect(existsSync(join(home,'.ssh'))).toBe(true);
    expect(await cleanupKeypair(opts,prepared(key),{all_resources_destroyed:true},env(home))).toEqual({mode:'managed',cleaned:true});
  }finally{rmSync(home,{recursive:true,force:true});}
});
test('failed prepared acknowledgement preserves generated files and refuses fresh adoption',async()=>{
  const home=temporary();try{
    await expect(prepareKeypair(opts,fresh,env(home),()=>true,()=>{throw new Error('private-diagnostic');})).rejects.toThrow('SSH key ownership update failed');
    expect(existsSync(join(home,'.ssh','demo'))).toBe(true);expect(existsSync(join(home,'.ssh','demo.pub'))).toBe(true);expect(existsSync(join(home,'.ssh','.demo.colors-key.lock'))).toBe(false);
    await expect(prepareKeypair(opts,fresh,env(home),()=>true,()=>true)).rejects.toThrow('unowned SSH key files exist');
  }finally{rmSync(home,{recursive:true,force:true});}
});
test('callback refusal happens before key generation and cancellation releases own reservation',async()=>{
  const home=temporary();try{
    await expect(prepareKeypair(opts,fresh,env(home),()=>1,()=>true)).rejects.toThrow('SSH key ownership update failed');expect(existsSync(join(home,'.ssh','demo'))).toBe(false);
    await expect(prepareKeypair(opts,fresh,env(home),()=>{throw new DOMException('cancelled','AbortError');},()=>true)).rejects.toThrow('cancelled');expect(existsSync(join(home,'.ssh','.demo.colors-key.lock'))).toBe(false);
  }finally{rmSync(home,{recursive:true,force:true});}
});
test('prepared ownership rejects missing pairs and mismatched fingerprints',async()=>{
  const home=temporary();try{
    await expect(prepareKeypair(opts,{status:'prepared',fingerprint:'SHA256:'+'A'.repeat(43)},env(home),()=>true,()=>true)).rejects.toThrow('owned SSH keypair is missing');
    const key=await prepareKeypair(opts,fresh,env(home),()=>true,()=>true);
    await expect(prepareKeypair(opts,{status:'prepared',fingerprint:'SHA256:'+'A'.repeat(43)},env(home),()=>true,()=>true)).rejects.toThrow('SSH key fingerprint differs from ownership');
    unlinkSync(key.private_key_path);
    expect(await cleanupKeypair(opts,prepared(key),{all_resources_destroyed:true},env(home))).toEqual({mode:'managed',cleaned:true});expect(existsSync(key.public_key_path)).toBe(false);
  }finally{rmSync(home,{recursive:true,force:true});}
});
test('cleanup needs literal destruction authority and removes only owned known-host file',async()=>{
  const home=temporary();try{
    const key=await prepareKeypair(opts,fresh,env(home),()=>true,()=>true);const known=join(home,'.ssh','demo.known_hosts');writeFileSync(known,'host');
    await expect(cleanupKeypair(opts,prepared(key),{all_resources_destroyed:1},env(home))).rejects.toThrow('SSH key cleanup requires complete resource destruction');expect(existsSync(key.private_key_path)).toBe(true);
    await cleanupKeypair(opts,prepared(key),{all_resources_destroyed:true},env(home));expect(existsSync(known)).toBe(true);
    await cleanupKeypair(opts,prepared(key),{all_resources_destroyed:true,known_hosts_owned:true},env(home));expect(existsSync(known)).toBe(false);
  }finally{rmSync(home,{recursive:true,force:true});}
});
test('external and build modes do not touch files or callbacks',async()=>{
  const home=temporary();try{
    const external={...opts,'vultr-ssh-keys':['operator-key'],'ssh-private-key-path':'/operator/key'};
    expect(await prepareKeypair(external,{status:'error'},env(home),()=>{throw new Error('unexpected');},()=>false)).toEqual({mode:'external',setting:'vultr-ssh-keys',reference:['operator-key'],private_key_path:'/operator/key'});
    expect((await cleanupKeypair(external,null,null,env(home))).mode).toBe('external');
    const planned=await prepareKeypair({...opts,'red/event':'build'},null,env(home),()=>false,()=>false);expect(planned.private_key_path).toBe('$HOME/.ssh/demo');expect(existsSync(join(home,'.ssh'))).toBe(false);
    for(const value of [null,'',[],[false],'REPLACE_ME'])await expect(prepareKeypair({...opts,'vultr-ssh-keys':value},fresh,env(home),()=>true,()=>true)).rejects.toThrow('invalid external SSH key reference');
  }finally{rmSync(home,{recursive:true,force:true});}
});
test('symlink keys and SSH directories are refused before generation',async()=>{
  const home=temporary(),target=temporary();try{
    symlinkSync(target,join(home,'.ssh'));await expect(prepareKeypair(opts,fresh,env(home),()=>true,()=>true)).rejects.toThrow('unsafe SSH key path');unlinkSync(join(home,'.ssh'));
    mkdirSync(join(home,'.ssh'));symlinkSync(join(target,'missing'),join(home,'.ssh','demo'));await expect(prepareKeypair(opts,fresh,env(home),()=>true,()=>true)).rejects.toThrow('unsafe SSH key path');
  }finally{rmSync(home,{recursive:true,force:true});rmSync(target,{recursive:true,force:true});}
});
test('cross-invocation reservation spans async acknowledgement and never removes competitors lock',async()=>{
  const home=temporary();let entered!:()=>void;const started=new Promise<void>(resolve=>{entered=resolve;});let finish!:()=>void;const wait=new Promise<void>(resolve=>{finish=resolve;});
  try{
    const first=prepareKeypair(opts,fresh,env(home),async()=>{entered();await wait;return true;},()=>true);await started;
    await expect(prepareKeypair(opts,fresh,env(home),()=>true,()=>true)).rejects.toThrow('SSH key operation reserved; explicit recovery required');expect(existsSync(join(home,'.ssh','.demo.colors-key.lock'))).toBe(true);finish();await first;
    expect(existsSync(join(home,'.ssh','.demo.colors-key.lock'))).toBe(false);
  }finally{finish();rmSync(home,{recursive:true,force:true});}
});
