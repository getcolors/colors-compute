import descriptors from '../resources/compute-options.json';
type Map=Record<string,any>;
const object=(v:any):v is Map=>v!==null&&typeof v==='object'&&!Array.isArray(v);
export function applyOptions(provider:string,stage:string,request:Map,documents:Map,opts:Map={}):Map {
 const descriptor=(descriptors as Map)[provider];request=structuredClone(request);if(!Object.hasOwn(request,'backups')&&descriptor?.backups_option&&Object.hasOwn(opts,descriptor.backups_option))request.backups={enabled:opts[descriptor.backups_option]};
 const keys=['backups','ipv6'].filter(k=>Object.hasOwn(request,k));if(!keys.length)return documents;
 if(!Object.hasOwn(descriptors,provider))throw Error('unsupported compute options capability');
 if(keys.includes('ipv6')&&!descriptor.ipv6_field)throw Error('unsupported compute options capability');
 if(keys.includes('ipv6')&&typeof request.ipv6!=='boolean')throw Error('invalid compute IPv6 policy');
 const backup=request.backups;
 if(keys.includes('backups')){if(!object(backup)||typeof backup.enabled!=='boolean'||Object.keys(backup).sort().join(',')!==(backup.enabled&&descriptor.schedule_field?'enabled,schedule':'enabled'))throw Error('invalid compute backup policy');if(backup.enabled&&descriptor.schedule_field){const schedule=backup.schedule;if(!object(schedule)||Object.keys(schedule).sort().join(',')!=='hour,type'||schedule.type!=='daily'||!Number.isSafeInteger(schedule.hour)||schedule.hour<0||schedule.hour>23)throw Error('invalid compute backup schedule');}}
 if(stage!=='node')return documents;const result=structuredClone(documents),resources=Object.values(result).map(doc=>doc.resource?.[descriptor.resource_type]?.[descriptor.resource_name]).filter(v=>v!==undefined);if(resources.length!==1)throw Error('compute options resource unavailable');const resource=resources[0];
 if(keys.includes('ipv6'))resource[descriptor.ipv6_field]=request.ipv6;if(backup!==undefined){resource[descriptor.backups_field]=descriptor.backups_values[String(backup.enabled)];if(backup.enabled&&descriptor.schedule_field)resource[descriptor.schedule_field]=[{type:'daily',hour:backup.schedule.hour}];}return result;
}
