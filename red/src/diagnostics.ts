import {accessSync,constants,statSync} from 'node:fs';
import {delimiter,join} from 'node:path';
import {mode} from './ssh.ts';
type Map=Record<string,any>;
const messages:Record<string,[string,string]>={
 'missing-tool':['Required infrastructure tools are missing from PATH.','Install the listed tools or load the deployment environment, then retry.'],
 'state-unreadable':['Cannot read infrastructure state; absence has not been established.','Check backend access and credentials before retrying. Do not remove state or ownership records.'],
 'failed-operation-without-state':['A previous infrastructure operation failed and its state file is missing. Cloud resources may still exist.','Inspect provider resources and reconcile the failed operation using the reviewed recovery procedure before retrying.'],
 'recorded-state-missing':['The ownership journal records infrastructure whose state file is missing.','Inspect provider resources and recover the recorded state before retrying. Do not reset the journal.'],
};
export class LifecycleDiagnostic extends Error {
 diagnostic:Map;
 constructor(code:string,tools:string[]=[]){
  super(code);const [message,hint]=messages[code];this.diagnostic={code,message,hint};
  if(code==='missing-tool')this.diagnostic.tools=[...new Set(tools)].filter(t=>['tofu','aws','gcloud','oci','ssh-keygen'].includes(t)).sort();
 }
 result(){const d=this.diagnostic,missing=d.tools?.length?' Missing: '+d.tools.join(', ')+'.':'';
  return {status:'error',errors:[d.message+missing+' Next: '+d.hint],diagnostics:[d]};}
}
export function requiredTools(opts:Map):string[]{
 const tools=['tofu'],tool=({s3:'aws',r2:'aws',gcs:'gcloud',oci:'oci'} as Map)[opts['provider-backend']??'r2'];
 if(tool)tools.push(tool);if(opts['provider-compute']==='oci')tools.push('oci');
 if(mode(opts).mode==='managed')tools.push('ssh-keygen');return [...new Set(tools)].sort();
}
export function missingTools(opts:Map,environment:Map):string[]{
 const paths=Object.hasOwn(environment,'PATH')?environment.PATH.split(delimiter):[];
 return requiredTools(opts).filter(tool=>!paths.some((entry:string)=>{
  try{const path=join(entry||'.',tool);if(!statSync(path).isFile())return false;accessSync(path,constants.X_OK);return true;}catch{return false;}
 }));
}
