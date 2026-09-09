import {expect, test} from 'bun:test';
import {cpSync, mkdtempSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import templates from '../resources/templates.json';
import {provider_plan} from '../src/index.ts';
const input = {
  name:'example-0',region:'ewr',plan:'vc2-2c-4gb',os_id:2284,firewall_group_id:'firewall',vpc_ids:['network'],
  ssh_key_ids:['key'],prevent_destroy:true,node_id:'0',user:'root',sudoer:'root',ssh_key_id:'key',
};
test('bundle provides eight data-selected providers and rejects prototype keys', () => {
  expect(Object.keys(templates).sort()).toEqual(['aws','azure','digitalocean','google','hcloud','oci','vultr','yandex']);
  expect(() => provider_plan('missing','node',{})).toThrow('compute provider templates unavailable: missing');
  expect(() => provider_plan('__proto__','node',{})).toThrow('compute provider templates unavailable: __proto__');
  expect(() => provider_plan('vultr','constructor',{})).toThrow('unsupported compute stage: constructor');
});
test('renders Vultr output with typed values and protects source template data', () => {
  const first = provider_plan('vultr','node',input) as any;
  expect(first['node.tf.json'].resource.vultr_instance.node.os_id).toBe(2284);
  expect(first['node.tf.json'].output.params.value.ip).toBe('${vultr_instance.node.main_ip}');
  first['node.tf.json'].resource.vultr_instance.node.vpc_ids.push('mutation');
  first['node.tf.json'].output.params.value.ip = 'mutation';
  const second = provider_plan('vultr','node',input) as any;
  expect(second['node.tf.json'].resource.vultr_instance.node.vpc_ids).toEqual(['network']);
  expect(second['node.tf.json'].output.params.value.ip).toBe('${vultr_instance.node.main_ip}');
  expect(Object.isFrozen(templates.vultr.node)).toBe(true);
});
test('copied distributable loads provider templates outside repository', async () => {
  const destination = mkdtempSync(join(tmpdir(),'colors-compute-red-package-'));
  const packageRoot = new URL('../', import.meta.url);
  try {
    for (const path of ['src','resources','package.json']) cpSync(new URL(path,packageRoot),join(destination,path),{recursive:true});
    // Dependencies have already been installed from the pinned manifest. Reuse
    // those here; source and JSON resource paths resolve only inside the copy.
    symlinkSync(new URL('node_modules',packageRoot).pathname,join(destination,'node_modules'),'dir');
    writeFileSync(join(destination,'check.ts'),`import {provider_plan} from './src/index.ts'; console.log(JSON.stringify(provider_plan('vultr','node',${JSON.stringify(input)})));`);
    const process = Bun.spawn([Bun.which('bun') ?? Bun.argv[0],join(destination,'check.ts')],{cwd:destination,stdout:'pipe',stderr:'pipe'});
    const stdout = await new Response(process.stdout).text();
    const stderr = await new Response(process.stderr).text();
    expect(await process.exited).toBe(0);
    expect(stderr).toBe('');
    expect(JSON.parse(stdout)['node.tf.json'].output.params.value.provider).toBe('vultr');
  } finally {rmSync(destination,{recursive:true,force:true});}
});
