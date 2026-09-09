import recipes from '../resources/provider-recipes.json';
import {expand,collect,state_keys,registry} from './index.ts';
import {deployment_requests} from './deployment-request.ts';
import {key_request} from './key-request.ts';
import {provider_request} from './provider-request.ts';
import {mode} from './ssh.ts';
type Map=Record<string,any>;
export function plan_deployment(input:Map,topology:Map[],requirements:Map) {
 const opts={...structuredClone(input),'red/event':'build'} as Map,selected=mode(opts);
 if(selected.mode==='managed')selected.public_key='ssh-ed25519 PLACEHOLDER managed-by-colors';
 const key=key_request(opts,selected,{}),assembly=deployment_requests(opts,topology,requirements,key),provider=opts['provider-compute'];
 const recipe=(recipes as Map)[provider],shared=structuredClone(recipe.planning_shared),sharedPlan=provider_request(opts,'shared',assembly.shared);
 const declarations=expand(topology);if(declarations.length>245)throw Error('build exceeds documentation address capacity');
 const entry=(registry.compute as Map)[provider],documents:Map={shared:sharedPlan.documents,nodes:{}},results:Map[]=[];
 const cidr=assembly.shared.network.subnet_cidr||assembly.shared.network.cidr||[...recipe.subnet_cidr_options,...recipe.network_cidr_options].map(name=>opts[name]).find(Boolean)||'10.0.0.0/24';
 const [address,prefixText]=cidr.split('/'),octets=address.split('.').map(Number),prefix=Number(prefixText);
 if(octets.length!==4||octets.some((n:number,i:number)=>!Number.isInteger(n)||n<0||n>255||String(n)!==address.split('.')[i])||!Number.isInteger(prefix)||prefix<0||prefix>32)throw Error('invalid compute network CIDR');
 const base=octets.reduce((n:number,p:number)=>n*256+p,0),size=2**(32-prefix);if(base%size)throw Error('invalid compute network CIDR');
 shared.params.network_cidr=cidr;
 for(const [ordinal,node] of assembly.nodes.entries()){
  documents.nodes[node.node_id]=provider_request(opts,'node',node,shared).documents;
  const offset=ordinal+10;if(offset>=size-1)throw Error('build exceeds private network address capacity');
  const ip=base+offset,params:Map={node_id:node.node_id,provider,name:node.name,ip:'192.0.2.'+offset,vpc_ip:[24,16,8,0].map(shift=>Math.floor(ip/2**shift)%256).join('.'),user:entry.user,sudoer:entry.sudoer};
  if(selected.mode==='managed')params.ssh_identity_file='$HOME/.ssh/'+opts.profile;else if(selected.private_key_path)params.ssh_identity_file=selected.private_key_path;
  results.push(params);
 }
 return {status:'planned',documents,shared,state_keys:state_keys(opts.profile,declarations.map(n=>n.node_id)),cluster:collect(declarations,results,declarations[0].node_id),key:{mode:selected.mode,...(selected.mode==='managed'?{private_key_path:'$HOME/.ssh/'+opts.profile}:selected.private_key_path?{private_key_path:selected.private_key_path}:{})}};
}
export function validate_deployment(input:Map,topology:Map[],requirements:Map):true {
 const opts={...structuredClone(input),'red/event':'build'} as Map,selected=mode(opts);
 if(selected.mode==='managed')selected.public_key='ssh-ed25519 PLACEHOLDER managed-by-colors';
 const assembly=deployment_requests(opts,topology,requirements,key_request(opts,selected,{}));
 const shared=structuredClone((recipes as Map)[opts['provider-compute']].planning_shared);
 provider_request(opts,'shared',assembly.shared);
 for(const node of assembly.nodes)provider_request(opts,'node',node,shared);
 return true;
}
