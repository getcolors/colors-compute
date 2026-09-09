import {copy} from './copy.ts';
import recipes from '../resources/provider-recipes.json';
import {expand,collect,state_keys,registry} from './index.ts';
import {deployment_requests} from './deployment-request.ts';
import {key_request} from './key-request.ts';
import {provider_request} from './provider-request.ts';
import {mode} from './ssh.ts';
type Map=Record<string,any>;
export function plan_deployment(input:Map,topology:Map[],requirements:Map) {
 const opts={...copy(input),'red/event':'build'} as Map,selected=mode(opts);
 if(selected.mode==='managed')selected.public_key='ssh-ed25519 PLACEHOLDER managed-by-colors';
 const key=key_request(opts,selected,{}),assembly=deployment_requests(opts,topology,requirements,key),provider=opts['provider-compute'];
 const recipe=(recipes as Map)[provider],shared=planningShared(recipe,opts,requirements),sharedPlan=provider_request(opts,'shared',assembly.shared);
 const declarations=expand(topology);if(declarations.length>245)throw Error('build exceeds documentation address capacity');
 const entry=(registry.compute as Map)[provider],documents:Map={shared:sharedPlan.documents,nodes:{}},results:Map[]=[];
 const noNetwork=assembly.shared.network.mode==='none';
 const cidr=(noNetwork?'10.0.0.0/24':null)||assembly.shared.network.subnet_cidr||assembly.shared.network.cidr||[...recipe.subnet_cidr_options,...recipe.network_cidr_options].map(name=>opts[name]).find(Boolean)||'10.0.0.0/24';
 const [address,prefixText]=cidr.split('/'),octets=address.split('.').map(Number),prefix=Number(prefixText);
 if(octets.length!==4||octets.some((n:number,i:number)=>!Number.isInteger(n)||n<0||n>255||String(n)!==address.split('.')[i])||!Number.isInteger(prefix)||prefix<0||prefix>32)throw Error('invalid compute network CIDR');
 const base=octets.reduce((n:number,p:number)=>n*256+p,0),size=2**(32-prefix);if(base%size)throw Error('invalid compute network CIDR');
 if(noNetwork){for(const key of ['vpc_id','vpc_ip_range','network_cidr','subnet_id','subnet_cidr'])delete shared.params[key];}else shared.params.network_cidr=cidr;
 if(Object.hasOwn(requirements,'endpoint'))shared.params.endpoint_ip='198.51.100.10';
 for(const [ordinal,node] of assembly.nodes.entries()){
  documents.nodes[node.node_id]=provider_request(opts,'node',node,shared).documents;
  const offset=ordinal+10;if(!noNetwork&&offset>=size-1)throw Error('build exceeds private network address capacity');
  const ip=base+offset,params:Map={provider_id:/^[0-9]+$/.test(recipe.planning_provider_id)?String(Number(recipe.planning_provider_id)+ordinal):recipe.planning_provider_id+'-'+node.node_id,node_id:node.node_id,provider,name:node.name,ip:'192.0.2.'+offset,vpc_ip:noNetwork?null:[24,16,8,0].map(shift=>Math.floor(ip/2**shift)%256).join('.'),user:entry.user,sudoer:entry.sudoer};
  if(selected.mode==='managed')params.ssh_identity_file='$HOME/.ssh/'+opts.profile;else if(selected.private_key_path)params.ssh_identity_file=selected.private_key_path;
  results.push(params);
 }
 if(requirements.roles){const roles=Object.fromEntries(declarations.map(n=>[n.node_id,n.role]));(assembly.shared as Map).peers=Object.fromEntries(results.map(n=>[n.node_id,{role:roles[n.node_id],vpc_ip:n.vpc_ip}]));documents.shared=provider_request(opts,'shared',assembly.shared).documents;}
 return {status:'planned',documents,shared,state_keys:state_keys(opts.profile,declarations.map(n=>n.node_id)),cluster:collect(declarations,results,assembly.entry_node_id),key:{mode:selected.mode,...(selected.mode==='managed'?{private_key_path:'$HOME/.ssh/'+opts.profile}:selected.private_key_path?{private_key_path:selected.private_key_path}:{})}};
}
export function validate_deployment(input:Map,topology:Map[],requirements:Map):true {
 const opts={...copy(input),'red/event':'build'} as Map,selected=mode(opts);
 if(selected.mode==='managed')selected.public_key='ssh-ed25519 PLACEHOLDER managed-by-colors';
 const assembly=deployment_requests(opts,topology,requirements,key_request(opts,selected,{}));
 const shared=planningShared((recipes as Map)[opts['provider-compute']],opts,requirements);
 provider_request(opts,'shared',assembly.shared);
 for(const node of assembly.nodes)provider_request(opts,'node',node,shared);
 return true;
}

function planningShared(recipe:Map,opts:Map,requirements:Map):Map{
 const shared=structuredClone(recipe.planning_shared);
 if(requirements.roles){shared.params.role_firewall_ids=Object.fromEntries(Object.keys(requirements.roles).map(r=>[r,'build-firewall-'+r]));if(recipe.role_tag_param)shared.params.role_tags=Object.fromEntries(Object.keys(requirements.roles).map(r=>[r,'colors-compute-'+opts.profile+'-'+r]));}
 return shared;
}
