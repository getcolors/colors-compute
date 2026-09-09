import {expect,test} from 'bun:test';
import {mkdtempSync,writeFileSync,readFileSync,rmSync,unlinkSync,symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {mode} from '../src/ssh.ts';
import {key_request} from '../src/key-request.ts';
import fixtures from '../../test/fixtures/provider-requests.json';
test('public file alias remains external, plans offline, refuses ambiguity and invalid files',()=>{
 const home=mkdtempSync(join(tmpdir(),'key-alias-')),opts={profile:'demo','provider-compute':'digitalocean','digitalocean-ssh-authorized-keys':'~/operator.pub'},prepared=mode(opts),path=join(home,'operator.pub'),publicKey=(fixtures[0].args[2] as any).key.public_key;
 try {
  expect(prepared.mode).toBe('external');expect(key_request({...opts,'red/event':'build'},prepared).ids).toEqual([Array(16).fill('00').join(':')]);
  writeFileSync(path,publicKey);const fingerprint=createHash('md5').update(Buffer.from(publicKey.split(/\s+/)[1],'base64')).digest('hex').match(/../g)!.join(':');
  expect(key_request(opts,prepared,{HOME:home})).toEqual({mode:'external',ids:[fingerprint],reference:fingerprint});expect(readFileSync(path,'utf8')).toBe(publicKey);
  expect(()=>mode({...opts,'digitalocean-ssh-keys':['123']})).toThrow('ambiguous');expect(()=>mode({...opts,'digitalocean-ssh-authorized-keys':[]})).toThrow('invalid external');
  writeFileSync(path,'not a public key');expect(()=>key_request(opts,prepared,{HOME:home})).toThrow('invalid external SSH public key file');
  unlinkSync(path);symlinkSync(join(home,'missing.pub'),path);expect(()=>key_request(opts,prepared,{HOME:home})).toThrow('regular .pub');
  expect(mode({profile:'demo','provider-compute':'digitalocean'})).toEqual({mode:'managed'});
 }finally{rmSync(home,{recursive:true,force:true});}
});
