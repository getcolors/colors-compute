/** Operator-invoked recovery of a failed initial shared AWS create. */
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Coordinator} from './coordinator.ts';
import {statePresence} from './execution.ts';
import {executeBackendCommand,type BackendRunner} from './backend.ts';
type Map=Record<string,any>;
const scans=[['describe-vpcs','Vpcs','tag:Name'],['describe-subnets','Subnets','tag:Name'],['describe-internet-gateways','InternetGateways','tag:Name'],['describe-route-tables','RouteTables','tag:Name'],['describe-security-groups','SecurityGroups','tag:Name'],['describe-key-pairs','KeyPairs','key-name']];
export async function recover_absent_aws_shared(opts:Map,operationId:string,environment:Map=process.env,runner:BackendRunner=executeBackendCommand,factory?:any){
 if(opts['provider-compute']!=='aws')throw Error('recovery requires AWS');
 const owner=factory?factory(opts,{environment,eventPrefix:'lifecycle/'}):new Coordinator(opts,{environment,eventPrefix:'lifecycle/'});
 await owner.acquire();
 try{
  const doc=(await owner.snapshot()).document,s=doc.shared;
  if(doc.status!=='active'||doc.key.phase!=='prepared'||s.phase!=='failed'||s.operation!=='create'||s.operation_id!==operationId||!Object.values(doc.nodes).every((n:any)=>n.phase==='declared'))throw Error('recovery does not match failed initial create');
  if((await statePresence(opts,opts.profile+'/compute/shared.tfstate',environment,runner)).status!=='absent')throw Error('recovery requires confirmed absent shared state');
  const env=Object.fromEntries(Object.entries(environment).filter(([k,v])=>typeof v==='string'&&!/^(COLORS_PAR_|TF_|TOFU_)/.test(k))) as Record<string,string>;
  Object.assign(env,{AWS_PAGER:'',AWS_CLI_AUTO_PROMPT:'off',AWS_MAX_ATTEMPTS:'1'});
  const directory=mkdtempSync(join(tmpdir(),'colors-recovery-'));
  try{for(const [operation,collection,filter] of scans){const r=await runner(['aws','ec2',operation!,'--region',opts['aws-region'],'--filters',`Name=${filter},Values=${opts.profile}*`,'--query',`length(${collection})`,'--output','json','--no-cli-pager'],{cwd:directory,env,timeoutMs:120000});if(r.exit!==0||JSON.parse(r.out)!==0)throw Error('recovery requires reviewed absent AWS resources');}}finally{rmSync(directory,{recursive:true,force:true});}
  await owner.transition('shared-retry',{evidence:'verified-provider-absence'});return {status:'recovered'};
 }finally{await owner.release();}
}

/** Explicit OCI retry after empty state and native resource absence checks. */
export async function recover_absent_oci_nodes(opts:Map,operations:Map,environment:Map=process.env,runner:BackendRunner=executeBackendCommand,factory?:any){
 const {ociClient,objectPath,availabilityDomains}=await import('./oci.ts');const {parseStateEnvelope}=await import('./backend.ts');
 if(opts['provider-compute']!=='oci'||opts['provider-backend']!=='oci'||!operations||Array.isArray(operations)||typeof operations!=='object'||!Object.keys(operations).length)throw Error('recovery requires OCI node operation IDs');
 const owner=factory?factory(opts,{environment,eventPrefix:'lifecycle/'}):new Coordinator(opts,{environment,eventPrefix:'lifecycle/'});
 await owner.acquire();try{
  const doc=(await owner.snapshot()).document;
  if(doc.status!=='active'||doc.key.phase!=='prepared'||doc.shared.phase!=='ready')throw Error('recovery requires owned OCI shared state');
  const request=await ociClient(opts,environment,runner);
  for(const [id,operationId] of Object.entries(operations)){
   const record=doc.nodes[id];if(record?.phase!=='failed'||record.operation!=='create'||record.operation_id!==operationId)throw Error('recovery does not match failed node create');
   const observed=await request('GET',objectPath(opts,record.state_key));
   if(observed!==null){const {document}=parseStateEnvelope(JSON.stringify(observed.data));if(document.resources.length||Object.keys(document.outputs).length)throw Error('recovery requires absent or empty node state');}
  }
  const inventory=await ociClient(opts,environment,runner,'iaas');
  const scopes:[string,Map][]=[['/20160918/instances',{}],...Array.from(new Set([opts['oci-availability-domain'],...availabilityDomains(opts)])).map(domain=>['/20160918/bootVolumes',{availabilityDomain:domain}] as [string,Map])];
  for(const [path,filters] of scopes){
   let page:string|undefined;do{
    const response=await inventory('GET',path,undefined,{...filters,compartmentId:opts['oci-compartment-id'],...(page?{page}:{})});const data=response?.data;
    if(!Array.isArray(data)||data.some((r:Map)=>(r.displayName??'').includes(opts.profile)&&r.lifecycleState!=='TERMINATED'))throw Error('recovery requires absent OCI instances and boot volumes');
    page=response!.headers['opc-next-page'];
   }while(page);
  }
  for(const id of Object.keys(operations))await owner.transition('retry',{node_id:id,evidence:'verified-provider-absence'});
  return {status:'recovered',nodes:Object.keys(operations).sort()};
 }finally{await owner.release();}
}
