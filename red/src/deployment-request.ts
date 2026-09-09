import recipes from '../resources/provider-recipes.json';
import {controller_artifact} from './controller.ts';
import {expand} from './index.ts';
type Map=Record<string,any>;
const safe=(v:any)=>typeof v==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/.exec(v)?.[0]===v;
const missing=(v:any)=>v==null||(typeof v==='string'&&(!v.trim()||v.trim().toUpperCase()==='REPLACE_ME'));
const object=(v:any):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
export function deployment_requests(opts:Map,topology:Map[],requirements:Map,key:Map) {
 const provider=opts['provider-compute'];
 if(typeof provider!=='string'||!Object.hasOwn(recipes,provider))throw Error('compute provider recipe unavailable');
 if(!object(requirements)||!Object.hasOwn(requirements,'security')||Object.keys(requirements).some(k=>!['security','network','single_host','legacy_state_keys','private','endpoint','roles','entry_node_id','kubernetes_controller','backups','ipv6'].includes(k)))throw Error('invalid deployment requirements');
 if(Object.hasOwn(requirements,'kubernetes_controller')){if(requirements.kubernetes_controller!==true)throw Error('invalid Kubernetes controller requirement');controller_artifact(opts,(recipes as any)[provider].planning_shared);}
 const single=Object.hasOwn(requirements,'single_host')?requirements.single_host:false;
 if(typeof single!=='boolean')throw Error('invalid single-host requirement');
 const nodes=expand(topology);
 if(nodes.length>1000||(single&&(nodes.length!==1||nodes[0].role!==null)))throw Error('invalid deployment topology');
 const names=new Set(nodes.map(n=>n.role).filter(r=>r!==null)),roles=requirements.roles;
 if(roles!=null){
  if(!object(roles)||Object.keys(roles).length!==names.size||Object.keys(roles).some(r=>!names.has(r))||nodes.some(n=>n.role===null))throw Error('invalid deployment role policies');
  for(const policy of Object.values(roles) as Map[]){
   if(!object(policy)||Object.keys(policy).length!==1||!Object.hasOwn(policy,'security')||!object(policy.security)||!Array.isArray(policy.security.ingress))throw Error('invalid deployment role policies');
   for(const rule of policy.security.ingress){if(!object(rule))throw Error('invalid deployment role policies');if(Object.hasOwn(rule,'peer_roles')&&(!Array.isArray(rule.peer_roles)||!rule.peer_roles.length||rule.peer_roles.some((r:any)=>!names.has(r))||new Set(rule.peer_roles).size!==rule.peer_roles.length))throw Error('invalid deployment peer roles');}
  }
 }
 const entry=Object.hasOwn(requirements,'entry_node_id')?requirements.entry_node_id:nodes[0].node_id;if(!nodes.some(n=>n.node_id===entry))throw Error('invalid deployment entry node');
 const settings=opts['compute-role-settings']??{};
 if(!object(settings)||Object.keys(settings).some(r=>!names.has(r))||Object.values(settings).some(v=>!object(v)||Object.keys(v).some(k=>!['size','image'].includes(k))||Object.values(v).some(missing)))throw Error('invalid compute role settings');
 const profile=opts.profile;if(!safe(profile))throw Error(':profile must be a safe identifier');
 const name=missing(opts[provider+'-name'])?profile:opts[provider+'-name'];if(!safe(name))throw Error('invalid compute name');
 const network=structuredClone(Object.hasOwn(requirements,'network')?requirements.network:{});
 if(!object(network))throw Error('invalid compute network request');
 if(!Object.hasOwn(network,'mode'))network.mode=single===true&&(!Object.hasOwn(requirements,'private')||requirements.private===false)&&(recipes as Map)[provider].network_modes?.includes('none')?'none':(recipes as Map)[provider].network_mode;
 if(network.mode==='none'&&(single!==true||(Object.hasOwn(requirements,'private')&&requirements.private!==false)))throw Error('network none requires public-only single host');
 const publicKey:Map={};for(const field of ['mode','public_key','ids','reference'])if(Object.hasOwn(key,field))publicKey[field]=structuredClone(key[field]);
 const base={...Object.fromEntries(["backups","ipv6"].filter(k=>Object.hasOwn(requirements,k)).map(k=>[k,structuredClone(requirements[k])])),...(roles!=null?{roles:structuredClone(roles)}:{}),key:publicKey,network,security:structuredClone(requirements.security),...(Object.hasOwn(requirements,'endpoint')?{endpoint:structuredClone(requirements.endpoint)}:{})};
 return {entry_node_id:entry,shared:{...structuredClone(base),node_id:'shared',name},nodes:nodes.map(node=>{
  const nodeName=single?name:name+'-'+node.node_id;if(!safe(nodeName))throw Error('invalid derived compute name');
  return {...structuredClone(base),node_id:node.node_id,name:nodeName,...(node.role!==null?{role:node.role}:{}),...(roles!=null?{security:structuredClone(roles[node.role!].security)}:{})};
 })};
}
export function source_cidrs(opts:Map,suffix:string,applicationKey?:string):string[] {
 if(typeof suffix!=='string'||/^[a-z][a-z0-9-]*$/.exec(suffix)?.[0]!==suffix)throw Error('invalid compute source setting');
 const value=applicationKey!==undefined&&Object.hasOwn(opts,applicationKey)?opts[applicationKey]:opts[String(opts['provider-compute'])+'-'+suffix];
 if(value==null)return [];
 if(typeof value==='string')return value.trim().split(/[\s,]+/).filter(Boolean);
 if(Array.isArray(value)&&value.every(v=>typeof v==='string'))return [...value];
 throw Error('invalid compute source list');
}
