import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;
export const bucketPath=(opts:Map)=>`/n/${encodeURIComponent(opts['oci-namespace'])}/b/${encodeURIComponent(opts['oci-bucket'])}`;
export const objectPath=(opts:Map,key:string)=>`${bucketPath(opts)}/o/${encodeURIComponent(key)}`;
export async function ociClient(opts:Map,environment:Map=process.env,runner:BackendRunner=executeBackendCommand,service:'objectstorage'|'iaas'='objectstorage'){
 if(!['objectstorage','iaas'].includes(service))throw Error('invalid OCI service');
 const env=Object.fromEntries(Object.entries(environment).filter(([k,v])=>typeof v==='string'&&!/^(COLORS_PAR_|TF_|TOFU_)/.test(k))) as Record<string,string>;
 return async(method:string,path:string,body?:unknown,query:Map={},headers:Map={}):Promise<Map|null>=>{
  const url=new URL(`https://${service}.${opts['oci-region']}.oraclecloud.com${path}`);for(const [k,v] of Object.entries(query))url.searchParams.set(k,String(v));
  const args=['oci','raw-request','--no-retry','--auth',(opts['oci-auth']??'SecurityToken')==='SecurityToken'?'security_token':'api_key','--profile',opts['oci-config-file-profile']??'DEFAULT','--region',opts['oci-region'],'--http-method',method,'--target-uri',String(url),'--request-headers',JSON.stringify(headers),...(body===undefined?[]:['--request-body',JSON.stringify(body)])];
  const result=await runner(args,{cwd:process.cwd(),env,timeoutMs:120000});if(result.exit)throw Error('OCI request failed');
  const response=JSON.parse(result.out),code=Number(response.status.split(' ')[0]);
  if(code===404)return null;if(code===409||code===412)return {conflict:true};if(code<200||code>=300)throw Error(`OCI request failed (${code})`);
  return {data:response.data,headers:Object.fromEntries(Object.entries(response.headers??{}).map(([k,v])=>[k.toLowerCase(),v]))};
 };
}

export function availabilityDomains(opts:Map):string[]{
 if(!Object.hasOwn(opts,'oci-availability-domains'))return [opts['oci-availability-domain']];
 const domains=opts['oci-availability-domains'];
 if(!Array.isArray(domains)||domains.length<1||domains.length>16||domains.some(d=>typeof d!=='string'||!/^[A-Za-z0-9][A-Za-z0-9:_-]{0,255}$/.test(d))||new Set(domains).size!==domains.length)throw Error('invalid OCI availability domains');
 return domains;
}
export function placeNode(opts:Map,stage:string,nodeId:unknown):Map {
 if(opts['provider-compute']==='oci'&&stage==='node')validateCpus(opts);
 if(opts['provider-compute']!=='oci'||!Object.hasOwn(opts,'oci-availability-domains'))return opts;
 const domains=availabilityDomains(opts);if(stage!=='node')return opts;
 const index=typeof nodeId==='string'?/(?:^|-)(0|[1-9][0-9]{0,2})$/.exec(nodeId):null;
 if(!index)throw Error('OCI placement requires an indexed node ID');
 return {...opts,'oci-availability-domain':domains[Number(index[1])%domains.length]};
}

export function validateCpus(opts:Map):void {
 const ocpus=opts['oci-ocpus'],vcpus=opts['oci-vcpus'],values=[ocpus,vcpus].filter(v=>v!=null);
 if(values.length!==1)throw Error('OCI requires exactly one of oci-ocpus or oci-vcpus');
 if(typeof values[0]!=='number'||!Number.isFinite(values[0])||values[0]<=0||(vcpus!=null&&!Number.isInteger(vcpus)))throw Error('invalid OCI CPU count');
}
