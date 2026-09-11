import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;
export const bucketPath=(opts:Map)=>`/n/${encodeURIComponent(opts['oci-namespace'])}/b/${encodeURIComponent(opts['oci-bucket'])}`;
export const objectPath=(opts:Map,key:string)=>`${bucketPath(opts)}/o/${encodeURIComponent(key)}`;
export async function ociClient(opts:Map,environment:Map=process.env,runner:BackendRunner=executeBackendCommand){
 const env=Object.fromEntries(Object.entries(environment).filter(([k,v])=>typeof v==='string'&&!/^(COLORS_PAR_|TF_|TOFU_)/.test(k))) as Record<string,string>;
 return async(method:string,path:string,body?:unknown,query:Map={},headers:Map={}):Promise<Map|null>=>{
  const url=new URL(`https://objectstorage.${opts['oci-region']}.oraclecloud.com${path}`);for(const [k,v] of Object.entries(query))url.searchParams.set(k,String(v));
  const args=['oci','raw-request','--no-retry','--auth',(opts['oci-auth']??'SecurityToken')==='SecurityToken'?'security_token':'api_key','--profile',opts['oci-config-file-profile']??'DEFAULT','--region',opts['oci-region'],'--http-method',method,'--target-uri',String(url),'--request-headers',JSON.stringify(headers),...(body===undefined?[]:['--request-body',JSON.stringify(body)])];
  const result=await runner(args,{cwd:process.cwd(),env,timeoutMs:120000});if(result.exit)throw Error('OCI request failed');
  const response=JSON.parse(result.out),code=Number(response.status.split(' ')[0]);
  if(code===404)return null;if(code===409||code===412)return {conflict:true};if(code<200||code>=300)throw Error(`OCI request failed (${code})`);
  return {data:response.data,headers:Object.fromEntries(Object.entries(response.headers??{}).map(([k,v])=>[k.toLowerCase(),v]))};
 };
}
