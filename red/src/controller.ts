import descriptors from '../resources/controllers.json';
type Map=Record<string,any>;
const object=(v:any):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const matches=(v:any,r:RegExp)=>typeof v==='string'&&r.exec(v)?.[0]===v;
export function controller_artifact(opts:Map,shared:Map) {
 const provider=opts['provider-compute'];
 if(typeof provider!=='string'||!Object.hasOwn(descriptors,provider))throw Error('unsupported compute Kubernetes controller capability');
 const d=(descriptors as Map)[provider];
 if(Object.hasOwn(opts,d.enabled_option)&&opts[d.enabled_option]!==true)throw Error('Kubernetes controller must be enabled');
 const key=d.version_options.find((k:string)=>opts[k]!=null),version=key===undefined?undefined:opts[key];
 if(!matches(version,/^v[0-9]+\.[0-9]+\.[0-9]+$/))throw Error('invalid Kubernetes controller version');
 const params=object(shared)?shared.params:undefined,network=object(params)?params[d.network_output]:undefined;
 if(!object(params)||params.provider!==provider||!matches(network,/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/))throw Error('invalid Kubernetes controller shared network');
 const name=opts[provider+'-name']??opts.profile;
 if(!matches(name,/^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/))throw Error('invalid Kubernetes controller cluster name');
 return {filename:'colors-compute-controller.yml',content:d.content.replaceAll('@VERSION@',version).replaceAll('@NETWORK_ID@',network),credentials:structuredClone(d.credentials),namespace:d.namespace,rollout_resource:d.rollout_resource,cluster_name:name};
}
