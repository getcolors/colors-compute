import {isIP} from 'node:net';
import {createHash} from 'node:crypto';
import recipeData from '../resources/provider-recipes.json';
import templateData from '../resources/templates.json';
import {registry} from './index.ts';
import {provider_plan} from './providers.ts';
type Map=Record<string,any>;
const recipes=structuredClone(recipeData) as Map;
const templates=templateData as Map;
const object=(value:unknown):value is Map=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const missing=(value:unknown)=>value==null||(typeof value==='string'&&(!value.trim()||value.trim().toUpperCase()==='REPLACE_ME'));
const fields=(value:unknown,required:string[],optional:string[]=[]):value is Map=>object(value)&&required.every(key=>Object.hasOwn(value,key))&&Object.keys(value).every(key=>[...required,...optional].includes(key));
const safe=(value:unknown)=>typeof value==='string'&&/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/.exec(value)?.[0]===value;
const integer=(value:unknown,lo:number,hi:number)=>typeof value==='number'&&Number.isInteger(value)&&value>=lo&&value<=hi;
function fail(message:string):never{throw new Error(message);}
interface Network {address:string;prefix:number;start:number;end:number}
function ipNumber(value:string):number{return value.split('.').reduce((number,part)=>number*256+Number(part),0);}
function cidr(value:unknown):Network {
  if(typeof value!=='string')fail('invalid compute network CIDR');
  const parts=value.split('/');const address=parts[0];const family=isIP(address);
  if(!family||parts.length>2)fail('invalid compute network CIDR');
  let prefix=parts.length===1?(family===4?32:128):Number(parts[1]);
  if(parts.length===2&&/^[0-9]+$/.exec(parts[1])?.[0]!==parts[1]) {
    if(family!==4||isIP(parts[1])!==4)fail('invalid compute network CIDR');
    let bits=ipNumber(parts[1]).toString(2).padStart(32,'0');
    if(/^1*0*$/.test(bits))prefix=bits.indexOf('0')===-1?32:bits.indexOf('0');
    else if(/^0*1*$/.test(bits))prefix=bits.indexOf('1')===-1?0:bits.indexOf('1');
    else fail('invalid compute network CIDR');
  }
  if(!Number.isInteger(prefix)||prefix<0||prefix>(family===4?32:128))fail('invalid compute network CIDR');
  if(family===6) {
    let text=address.split('%')[0];
    if(text.includes('.')) {const last=text.slice(text.lastIndexOf(':')+1);const n=ipNumber(last);text=text.slice(0,text.lastIndexOf(':')+1)+(Math.floor(n/65536)).toString(16)+':'+(n%65536).toString(16);}
    const halves=text.split('::');const left=halves[0]?halves[0].split(':'):[];const right=halves[1]?halves[1].split(':'):[];
    const groups=halves.length===2?[...left,...Array(8-left.length-right.length).fill('0'),...right]:left;
    const number=groups.reduce((n,part)=>(n<<16n)+BigInt('0x'+part),0n);
    if(number% (1n<<BigInt(128-prefix))!==0n)fail('invalid compute network CIDR');
    fail('unsupported compute network address family');
  }
  const start=ipNumber(address);const size=2**(32-prefix);
  if(start%size!==0)fail('invalid compute network CIDR');
  if(`${address}/${prefix}`!==value)fail('unsupported compute network address family');
  return {address,prefix,start,end:start+size-1};
}
function binding(spec:Map,context:Map):any {
  if(Object.hasOwn(spec,'value'))return structuredClone(spec.value);
  if(Object.hasOwn(spec,'path')) {
    let current:any=context;
    for(const key of spec.path.split('.'))current=object(current)&&Object.hasOwn(current,key)?current[key]:null;
    if(missing(current)) {
      if(Object.hasOwn(spec,'default'))return structuredClone(spec.default);
      fail('missing compute input: '+spec.path);
    }
    return structuredClone(current);
  }
  if(Object.hasOwn(spec,'first')) {
    for(const candidate of spec.first)try{return binding(candidate,context);}catch(error){if(!(error instanceof Error)||!error.message.startsWith('missing compute input: '))throw error;}
    fail('missing compute input: '+spec.first[0].path);
  }
  if(Object.hasOwn(spec,'list'))return spec.list.map((item:Map)=>binding(item,context));
  if(Object.hasOwn(spec,'object'))return Object.fromEntries(Object.entries(spec.object).map(([key,item])=>[key,binding(item as Map,context)]));
  fail('invalid provider recipe');
}
function rules(format:string,request:Map,network:string|null,name:string):Map {
  const expanded:[string,Map,string|null,boolean][]=[];
  for(const rule of [...request.security.ingress].sort((a,b)=>a.id<b.id?-1:a.id>b.id?1:0))for(const source of [...rule.sources].sort()) {
    const privateSource=source==='private';let range=privateSource?network:source;
    if(privateSource&&format==='digitalocean')range=null;
    else if(privateSource&&range===null)fail('missing compute network CIDR for private ingress');
    expanded.push([rule.id+':'+source,rule,range,privateSource]);
  }
  const result:Map={},publicIngress:Map={},privateIngress:Map={},publicRules:Map[]=[];
  for(const [ordinal,[key,rule,range,privateSource]] of expanded.entries()) {
    const lo=rule.from_port,hi=rule.to_port,protocol=rule.protocol;const icmp=protocol==='icmp';const ports=icmp?null:lo===hi?String(lo):`${lo}-${hi}`;
    switch(format) {
      case 'vultr': {const net=cidr(range);result[key]={protocol,port:ports,ip_type:'v4',subnet:net.address,subnet_size:net.prefix};break;}
      case 'aws':result[key]={protocol,from_port:icmp?-1:lo,to_port:icmp?-1:hi,cidr:range};break;
      case 'azure':if(ordinal>=3996)fail('too many compute ingress rules');result[key.replaceAll(':','-').replaceAll('/','-').replaceAll('.','-')]={priority:100+ordinal,protocol:protocol[0].toUpperCase()+protocol.slice(1),port:icmp?'*':ports,sources:[range]};break;
      case 'google':result[key]={name:name+'-'+createHash('sha256').update(key).digest('hex').slice(0,12),protocol,ports:icmp?[]:[ports],source_ranges:[range]};break;
      case 'yandex':result[key]={protocol:protocol.toUpperCase(),from_port:lo,to_port:hi,cidr_blocks:[range]};break;
      case 'digitalocean':(privateSource?privateIngress:publicIngress)[key]={protocol,...(icmp?{}:{port_range:ports}),...(!privateSource?{source_addresses:[range]}:{})};break;
      case 'hcloud':publicRules.push({direction:'in',protocol,...(icmp?{}:{port:ports}),source_ips:[range]});break;
      case 'oci':result[key]={direction:'INGRESS',protocol:icmp?'1':protocol==='tcp'?'6':'17',cidr:range,from_port:lo,to_port:hi};break;
      default:fail('unsupported compute firewall format');
    }
  }
  if(format==='oci')result['outbound-all']={direction:'EGRESS',protocol:'all',cidr:'0.0.0.0/0',from_port:null,to_port:null};
  const egress=format==='yandex'?{'all-ipv4':{protocol:'ANY',from_port:0,to_port:65535,cidr_blocks:['0.0.0.0/0']}}:{'all-ipv4':{protocol:'-1',from_port:null,to_port:null,cidr:'0.0.0.0/0'}};
  return {ingress:result,rules:result,egress,public_ingress:publicIngress,private_ingress:privateIngress,public_rules:publicRules,outbound_rules:['tcp','udp','icmp'].map(protocol=>({protocol,...(protocol!=='icmp'?{port_range:'1-65535'}:{}),destination_addresses:['0.0.0.0/0','::/0']}))};
}

/** Resolve provider-neutral requests through packaged data, without infrastructure I/O. */
export function provider_request(opts:Map,stage:string,request:Map,shared:Map|null=null) {
  const provider=object(opts)?opts['provider-compute']:null;
  if(typeof provider!=='string'||!Object.hasOwn(recipes,provider))fail('compute provider recipe unavailable');
  if(stage!=='shared'&&stage!=='node')fail('unsupported compute request stage');
  const recipe=recipes[provider],entry=(registry.compute as Map)[provider];
  if(!safe(opts.profile))fail('invalid compute profile');
  if(!fields(request,['node_id','key','network','security'],['name'])||!safe(request.node_id))fail('invalid compute request');
  const profile=opts.profile;const name=Object.hasOwn(request,'name')?request.name:stage==='shared'?profile:profile+'-'+request.node_id;
  if(!safe(name))fail('invalid compute name');
  const {key,network,security}=request;
  if(!fields(key,['mode'],['public_key','ids','reference'])||!['managed','external'].includes(key.mode))fail('invalid compute key request');
  if(!fields(network,['mode'],['cidr','subnet_cidr','zone','private_ip']))fail('invalid compute network request');
  if(network.mode!==recipe.network_mode)fail('unsupported compute network mode');
  if(!fields(security,['ingress','egress','private_filter'])||security.egress!=='all'||typeof security.private_filter!=='boolean')fail('unsupported compute security policy');
  if(security.private_filter&&!recipe.private_filter)fail('unsupported compute private filtering');
  if(!Array.isArray(security.ingress)||!security.ingress.length)fail('invalid compute ingress');
  const seen=new Set<string>();let hasSSH=false;
  for(const rule of security.ingress) {
    if(!fields(rule,['id','protocol','from_port','to_port','sources'])||!safe(rule.id)||seen.has(rule.id))fail('invalid compute ingress');seen.add(rule.id);
    if(!(rule.protocol==='icmp'&&rule.from_port===null&&rule.to_port===null||['tcp','udp'].includes(rule.protocol)&&integer(rule.from_port,1,65535)&&integer(rule.to_port,rule.from_port,65535)))fail('invalid compute ingress');
    if(!Array.isArray(rule.sources)||!rule.sources.length||rule.sources.some((source:any)=>typeof source!=='string')||new Set(rule.sources).size!==rule.sources.length)fail('invalid compute ingress');
    for(const source of rule.sources) {
      if(source==='private') {if(recipe.firewall_format==='hcloud')fail('unsupported compute private filtering');}
      else {cidr(source);hasSSH ||= rule.protocol==='tcp'&&rule.from_port<=22&&rule.to_port>=22;}
    }
  }
  if(!hasSSH)fail('compute public SSH ingress required');
  let networkCIDR=network.cidr;
  if(missing(networkCIDR))networkCIDR=recipe.network_cidr_options.map((name:string)=>opts[name]).find((value:any)=>!missing(value))??null;
  let subnet=network.subnet_cidr;
  if(missing(subnet))subnet=recipe.subnet_cidr_options.map((name:string)=>opts[name]).find((value:any)=>!missing(value))??networkCIDR;
  const parsed=networkCIDR!==null?cidr(networkCIDR):null;
  if(subnet!==null) {const parsedSubnet=cidr(subnet);if(parsed&&(parsedSubnet.start<parsed.start||parsedSubnet.end>parsed.end))fail('compute subnet must be inside network');}
  if(!missing(network.private_ip)) {
    if(!recipe.static_private_ip)fail('unsupported compute static private address');
    const value=network.private_ip;
    if(typeof value!=='string'||isIP(value)!==4||subnet===null)fail('invalid compute private address');
    const address=ipNumber(value),net=cidr(subnet);if(address<net.start||address>net.end)fail('invalid compute private address');
  }
  const protect=Object.hasOwn(opts,'compute-prevent-destroy')?opts['compute-prevent-destroy']:true;
  if(typeof protect!=='boolean')fail('invalid compute prevent-destroy flag');
  shared??={};if(!object(shared))fail('invalid compute shared results');
  if(stage==='node'&&(!object(shared.params)||shared.params.provider!==provider))fail('compute shared provider mismatch');
  const registrationOwned=key.mode==='managed'||recipe.registration_external===true;
  const primary=(registrationOwned?shared.ssh_key_id:key.reference)??null;
  const ids=registrationOwned&&primary!==null?[primary]:(Object.hasOwn(key,'ids')?key.ids:[]);
  if(!Array.isArray(ids)||ids.some(item=>!((typeof item==='string'&&!missing(item))||integer(item,1,Number.MAX_SAFE_INTEGER))))fail('invalid compute key references');
  if(stage==='node'&&entry.registration&&!ids.length)fail('compute key references required');
  const derived:Map={profile,name,node_id:request.node_id,prevent_destroy:protect,public_key:key.public_key??null,ssh_key_id:primary,ssh_key_ids:ids,key_name:Object.hasOwn(shared,'key_name')?shared.key_name:key.reference??null,user:entry.user,sudoer:entry.sudoer,network_cidr:networkCIDR,subnet_cidr:subnet,network_address:parsed?.address??null,network_prefix:parsed?.prefix??null,network_name:profile+'-network',subnet_name:profile+'-subnet',firewall_name:profile+'-firewall',network_tag:profile+'-network',deployment_tag:'colors-compute-'+profile,nic_name:name+'-nic',public_ip_name:name+'-public',...rules(recipe.firewall_format,request,networkCIDR,profile)};
  let image=opts['google-image-id'];
  if(missing(image)&&!missing(opts['google-image-project'])&&!missing(opts['google-image-family']))image='projects/'+opts['google-image-project']+'/global/images/family/'+opts['google-image-family'];
  derived.google_image=image??null;
  let selectedStage=stage==='shared'&&registrationOwned&&entry.registration?'shared-keygen':stage;
  if(stage==='node'&&recipe.discovery_stage&&missing(opts[recipe.image_option]))selectedStage=recipe.discovery_stage;
  const tokens=[...new Set([...JSON.stringify(templates[provider][selectedStage]).matchAll(/\{\{([a-z_]+)\}\}/g)].map(match=>match[1]))].sort();
  const context={opts,request,shared,derived};const inputs:Map={};
  for(const token of tokens) {
    if(!Object.hasOwn(recipe.bindings,token))fail('missing provider recipe binding: '+token);
    inputs[token]=token==='ssh_key_id'?structuredClone(primary):binding(recipe.bindings[token],context);
  }
  const checkLiterals=(value:any):void=>{
    if(typeof value==='string'&&(value.includes('${')||value.includes('%{')))fail('invalid compute literal');
    if(Array.isArray(value))value.forEach(checkLiterals);
    else if(object(value))for(const [key,item] of Object.entries(value)){checkLiterals(key);checkLiterals(item);}
  };
  checkLiterals(inputs);
  return {provider,stage:selectedStage,inputs,documents:provider_plan(provider,selectedStage,inputs)};
}
