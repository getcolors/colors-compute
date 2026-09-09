import {test,expect} from 'bun:test';
import fixtures from '../../test/fixtures/provider-roles.json';
import bases from '../../test/fixtures/provider-requests.json';
import {deployment_requests} from '../src/deployment-request.ts';
import {provider_request} from '../src/provider-request.ts';
import {plan_deployment} from '../src/planning.ts';
import {orchestrate} from '../src/orchestration.ts';
import {Coordinator} from '../src/coordinator.ts';
type Map=Record<string,any>;
const [opts,,base]:any=(bases as Map[]).find(c=>c.args[0]['provider-compute']==='vultr'&&c.args[1]==='shared')!.args;
const topology=[{role:'db',count:2},{role:'app',count:1}];
const ssh={id:'ssh',protocol:'tcp',from_port:22,to_port:22,sources:['192.0.2.0/24']};
const peer={id:'db',protocol:'tcp',from_port:5432,to_port:5432,peer_roles:['app']};
const policy={ingress:[ssh],egress:'all',private_filter:true};
const requirements={security:policy,network:base.network,entry_node_id:'app-0',roles:{db:{security:{...policy,ingress:[ssh,peer]}},app:{security:policy}}};
for(const c of fixtures as any[])test(c.name,()=>{const args=c.args;if(c.error)expect(()=>provider_request(args[0],args[1],args[2],args[3])).toThrow(c.error);else expect(provider_request(args[0],args[1],args[2],args[3])).toEqual(c.expected);});
test('entry, role sizes, exact peer rules and IPv6 use data-driven recipes',()=>{
 const req=structuredClone(requirements);req.roles.app.security.ingress.push({...ssh,id:'ssh-v6',sources:['2001:db8::/32']});
 const result=plan_deployment({...opts,'vultr-plan-db':'vc2-4c-8gb','compute-role-settings':{app:{size:'vc2-2c-4gb'}}},topology,req);
 expect(result.cluster.entry_node_id).toBe('app-0');expect(result.documents.nodes['db-0']['node.tf.json'].resource.vultr_instance.node.plan).toBe('vc2-4c-8gb');
 const rules=result.documents.shared['shared-roles.tf.json'].locals.ingress;
 expect(rules['db:db:peer:app-0']).toMatchObject({subnet_size:32,subnet:result.cluster.nodes[2].vpc_ip,role:'db'});
 expect(rules['app:ssh-v6:2001:db8::/32'].ip_type).toBe('v6');
 for(const change of [{entry_node_id:'absent'},{roles:{}},{roles:{db:{security:{...policy,ingress:[ssh,{...peer,peer_roles:['unknown']}]}},app:{security:policy}}}])expect(()=>plan_deployment(opts,topology,{...requirements,...change})).toThrow();
 const request:any=deployment_requests(opts,topology,requirements,base.key).shared;
 expect(Object.keys(provider_request(opts,'shared',request).inputs.role_ingress).some(k=>k.includes(':peer:'))).toBe(false);
 request.peers={'app-0':{role:'app',vpc_ip:'10.42.1.5'}};const before=provider_request(opts,'shared',request);request.peers['app-0'].vpc_ip='10.42.1.6';
 expect(Object.keys(provider_request(opts,'shared',request).inputs.role_ingress)).toEqual(Object.keys(before.inputs.role_ingress));
});
test('native SDK waits for siblings then updates peers, preserving them on reconverge',async()=>{
 let observed:any={status:'absent'},writes=0;const states:Map={},events:string[]=[],sharedPlans:Map[]=[];let failNode=false;
 const deps:any={compute_credential_errors:()=>[],validate_deployment:()=>true,registration_preflight:()=>({status:'checked'}),
 coordinator:(o:Map,config:Map)=>new Coordinator(o,{...config,read:async()=>structuredClone(observed),write:async(intent:any)=>{if(intent.condition.if_match&&intent.condition.if_match!==observed.etag)return {status:'conflict'};observed={status:'present',etag:'e'+(++writes),document:structuredClone(intent.document)};return {status:'written',etag:observed.etag};}}),
 prepare_keypair:async(o:any,owned:any,env:any,intent:any,prepared:any)=>{if(owned.status==='fresh'){await intent();await prepared('SHA256:'+'A'.repeat(43));}return {mode:'managed',public_key:'ssh-ed25519 public'};},
 state_presence:async(o:any,key:string)=>({status:Object.hasOwn(states,key)?'present':'absent'}),read_state:async(o:any,key:string)=>({status:'present',params:states[key].params,outputs:states[key]}),
 converge_state:async(o:any,key:string,docs:any)=>{
  let outputs:any;if(key.endsWith('/shared.tfstate')){events.push('shared');sharedPlans.push(docs['shared-roles.tf.json'].locals.ingress);outputs={ssh_key_id:'test-key',params:{provider:'vultr',vpc_id:'test-vpc',role_firewall_ids:{db:'db-fw',app:'app-fw'}}};}
  else{const id=key.split('/').at(-1)!.replace('.tfstate','');await Bun.sleep(id==='db-0'?8:1);events.push(id);if(failNode&&id==='db-0')return {status:'error'};outputs={params:{node_id:id,provider:'vultr',name:docs['node.tf.json'].resource.vultr_instance.node.label,ip:'192.0.2.10',vpc_ip:'10.42.1.'+({'db-0':10,'db-1':11,'app-0':12} as Map)[id],user:'root',sudoer:'root'}};}
  states[key]=outputs;return {status:'ready',params:outputs.params,outputs};
 }};
 const input={...opts,profile:'demo','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1','red/event':'create'};
 let result=await orchestrate(input,topology,requirements,{},deps);expect(result.status).toBe('ready');expect(result.cluster.entry_node_id).toBe('app-0');expect(events[0]).toBe('shared');expect(events.at(-1)).toBe('shared');expect(sharedPlans[0]['db:db:peer:app-0']).toBeUndefined();expect(sharedPlans[1]['db:db:peer:app-0'].subnet).toBe('10.42.1.12');
 result=await orchestrate(input,topology,requirements,{},deps);expect(result.status).toBe('ready');expect(sharedPlans[2]['db:db:peer:app-0'].subnet).toBe('10.42.1.12');expect(observed.document.lock.state).toBe('idle');
 failNode=true;const prior=sharedPlans.length;expect(await orchestrate(input,topology,requirements,{},deps)).toEqual({status:'error'});expect(sharedPlans).toHaveLength(prior+1);expect(events.at(-1)).toBe('db-0');
});
test('DigitalOcean roles select separate tags and retain explicit peer addresses',()=>{
 const [input,,request]:any=(bases as Map[]).find(c=>c.args[0]['provider-compute']==='digitalocean'&&c.args[1]==='shared')!.args;
 const req={...requirements,network:request.network};
 const result=plan_deployment(input,topology,req);
 const shared:any=Object.values(result.documents.shared).find((d:any)=>d.resource?.digitalocean_firewall);
 expect(result.shared.params.role_tags).toEqual({db:'colors-compute-example-db',app:'colors-compute-example-app'});
 const node=result.documents.nodes['db-0']['node.tf.json'].resource.digitalocean_droplet.node;
 expect(node.tags).toContain('colors-compute-example-db');
 expect(JSON.stringify(shared)).toContain(result.cluster.nodes[2].vpc_ip+'/32');
 expect(JSON.stringify(shared)).toContain('peer:app-0');
});
test('peer identity and address cannot escape declared roles or private network',()=>{
 const request:any=deployment_requests(opts,topology,requirements,base.key).shared;
 for(const [id,role,ip] of [['app-0','app','192.0.2.1'],['db-0','app','10.42.1.2'],['app-01','app','10.42.1.2'],['app-0','app','10.42.0.0'],['app-0','app','10.42.255.255']])expect(()=>provider_request(opts,'shared',{...request,peers:{[id]:{role,vpc_ip:ip}}})).toThrow();
 for(const entry of [null,[],{},1])expect(()=>deployment_requests(opts,topology,{...requirements,entry_node_id:entry},base.key)).toThrow('entry');
 for(const security of [null,[],{ingress:null},{ingress:[null]}])expect(()=>deployment_requests(opts,topology,{...requirements,roles:{...requirements.roles,db:{security}}},base.key)).toThrow('role policies');
});
