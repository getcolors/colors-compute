import {test,expect} from 'bun:test';
import {mkdtempSync,writeFileSync,rmSync,symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {deployment_requests,key_request} from '../src/index.ts';
import fixtures from '../../test/fixtures/provider-requests.json';
const opts={'profile':'demo','provider-compute':'vultr'};
const key={mode:'managed',public_key:'public',private_key_path:'/private'};
const req={security:{ingress:[],egress:'all',private_filter:false}};
const pub=(fixtures[0].args[2] as any).key.public_key;
test('stable scale names, detached public-only requests and single-host names',()=>{
 const a=deployment_requests(opts,[{count:1}],req,key),b=deployment_requests(opts,[{count:3}],req,key);
 expect(a.nodes[0]).toEqual(b.nodes[0]);expect(a.nodes[0].name).toBe('demo-0');expect(a.shared.key).not.toHaveProperty('private_key_path');
 a.nodes[0].security.ingress.push('changed');expect(req.security.ingress).toEqual([]);expect(a.shared.security.ingress).toEqual([]);
 expect(deployment_requests(opts,[{count:1}],{...req,single_host:true},key).nodes[0].name).toBe('demo');
 expect(deployment_requests({...opts,'vultr-name':'custom'},[{role:'server',count:1}],req,key).nodes[0].name).toBe('custom-server-0');
 expect(()=>deployment_requests(opts,[{count:2}],{...req,single_host:true},key)).toThrow('topology');
 expect(()=>deployment_requests({...opts,'vultr-name':'x'.repeat(63)},[{count:1}],req,key)).toThrow('derived compute name');
});
test('key IDs stay external and public content validated',()=>{
 expect(key_request({'provider-compute':'hcloud'},{mode:'external',reference:[1,'key']})).toEqual({mode:'external',ids:[1,'key'],reference:1});
 expect(()=>key_request({'provider-compute':'hcloud'},{mode:'external',reference:[true]})).toThrow('reference');
 expect(key_request({'provider-compute':'yandex'},{mode:'external',reference:pub})).toEqual({mode:'external',public_key:pub});
 expect(()=>key_request({'provider-compute':'yandex'},{mode:'external',reference:'/private'})).toThrow('public key');
});
test('public file only, strict encoding, planning never reads files',()=>{
 const dir=mkdtempSync(join(tmpdir(),'red-key-'));try{
 writeFileSync(join(dir,'key.pub'),pub);writeFileSync(join(dir,'private'),'secret');symlinkSync(join(dir,'key.pub'),join(dir,'link.pub'));
 const opts={'provider-compute':'aws'};
 expect(key_request(opts,{mode:'external',reference:'~/key.pub'},{HOME:dir}).public_key).toBe(pub);
 for(const name of ['private','link.pub','missing.pub'])expect(()=>key_request(opts,{mode:'external',reference:'~/'+name},{HOME:dir})).toThrow('regular .pub');
 writeFileSync(join(dir,'bad.pub'),Buffer.from([255]));expect(()=>key_request(opts,{mode:'external',reference:'~/bad.pub'},{HOME:dir})).toThrow('public key file');
 expect(key_request({...opts,'red/event':'build'},{mode:'external',reference:'/missing.pub'}).public_key).toContain('PLACEHOLDER');
 }finally{rmSync(dir,{recursive:true,force:true});}
});
