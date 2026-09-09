import {test,expect} from 'bun:test';
import {plan_deployment,source_cidrs} from '../src/index.ts';
import fixtures from '../../test/fixtures/provider-requests.json';
for(const provider of ['aws','azure','digitalocean','google','hcloud','oci','vultr','yandex'])test('credential-free deterministic '+provider,()=>{
 const args=fixtures.find((f:any)=>f.args[0]['provider-compute']===provider&&f.args[1]==='shared')!.args as any;
 const requirements={network:args[2].network,security:args[2].security},topology=[{count:3}];
 const result=plan_deployment(args[0],topology,requirements);
 expect(result.cluster.nodes.map((n:any)=>n.ip)).toEqual(['192.0.2.10','192.0.2.11','192.0.2.12']);expect(result.status).toBe('planned');expect(result).toEqual(plan_deployment(args[0],topology,requirements));
});
test('source list normalization',()=>{expect(source_cidrs({'p-ssh':'a, b\nc','provider-compute':'p'},'ssh')).toEqual(['a','b','c']);expect(source_cidrs({},'ssh')).toEqual([]);expect(()=>source_cidrs({'p-ssh':[2],'provider-compute':'p'},'ssh')).toThrow('source list');});
test('native workflow options can carry callbacks without entering compute documents',async()=>{
 const {run,workflow}=await import('red/workflow');
 const args=fixtures.find((f:any)=>f.args[0]['provider-compute']==='vultr'&&f.args[1]==='shared')!.args as any;
 const requirements={network:args[2].network,security:args[2].security};
 let planned:any;
 await run(workflow({start:'start',wireFn:()=>[(opts:any)=>{planned=plan_deployment(opts,[{count:1}],requirements);return opts;}]}),{...args[0],'red/callback':()=>true});
 expect(planned?.status).toBe('planned');
 expect(JSON.stringify(planned.documents)).not.toContain('red/callback');
});
