import {managedCoordination,managedDocumentValid} from './managed-journal.ts';
import {lifecycle,lifecycleDocumentValid} from './lifecycle.ts';
import {registry, expand, state_keys} from './index.ts';
type Map = Record<string, any>;
const object = (value: unknown): value is Map => value !== null && typeof value === 'object' && !Array.isArray(value);
const fields = (value: unknown, required: string[], optional: string[] = []): value is Map => object(value) && required.every(k => Object.hasOwn(value,k)) && Object.keys(value).every(k => [...required,...optional].includes(k));
const match = (value: unknown, pattern: RegExp): value is string => typeof value === 'string' && pattern.exec(value)?.[0] === value;
const safe = (value: unknown) => match(value,/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/);
const nonblank = (value: unknown) => typeof value === 'string' && !!value.trim();
const integer = (value: unknown, min: number, max: number) => typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max;
const roleValid = (value: unknown) => value === null || match(value,/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/);
export function identityValid(value: unknown): value is Map {
  if (!fields(value,['profile','provider','backend']) || !safe(value.profile) || typeof value.provider !== 'string' || !Object.hasOwn(registry.compute,value.provider)) return false;
  const backend=value.backend;
  if (!fields(backend,['kind','bucket','region'],['endpoint']) || !match(backend.bucket,/^[a-z0-9][a-z0-9.-]{0,62}$/) || !safe(backend.region)) return false;
  if ((backend.kind === 's3' || backend.kind === 'gcs')) return !Object.hasOwn(backend,'endpoint');
  return (backend.kind === 'oci' || (backend.kind === 'r2' && backend.region === 'auto')) && Object.hasOwn(backend,'endpoint') && match(backend.endpoint,/^https:\/\/[a-zA-Z0-9.-]+(:[0-9]{1,5})?\/?$/);
}
function topologyValid(value: unknown): value is Map[] {
  if (!Array.isArray(value) || !value.length || value.length > 1000) return false;
  const roles=new Set<string|null>();let total=0;
  for(const declaration of value) {
    if (!fields(declaration,[],['role','count'])) return false;
    const role=declaration.role ?? null;
    const count=Object.hasOwn(declaration,'count') ? declaration.count : 1;
    if (!roleValid(role) || roles.has(role) || (role === null && value.length !== 1) || !integer(count,1,1000)) return false;
    roles.add(role);total+=count;
    if (total>1000 || !safe(role === null ? String(count-1) : `${role}-${count-1}`)) return false;
  }
  return true;
}
function eventValid(value: unknown): value is Map {
  if (!object(value) || typeof value.type !== 'string') return false;
  const extra: Record<string,string[]>={acquire:[],declare:['topology'],start:['node_id','operation_id'],complete:['node_id','operation_id'],fail:['node_id','operation_id'],release:[]};
  if (!Object.hasOwn(extra,value.type) || !fields(value,['type','run_id','write_id','target_etag',...extra[value.type]])) return false;
  if (!safe(value.run_id) || !safe(value.write_id) || !(value.target_etag === null || nonblank(value.target_etag))) return false;
  if (value.type === 'declare') return topologyValid(value.topology);
  if (extra[value.type].includes('node_id')) return safe(value.node_id) && safe(value.operation_id);
  return true;
}
function observationValid(value: unknown): value is Map {
  if (!object(value)) return false;
  if(value.status === 'absent' || value.status === 'error') return fields(value,['status']);
  return value.status === 'present' && fields(value,['status','etag','document']) && nonblank(value.etag);
}
export function documentValid(value: unknown): value is Map {
  if(object(value)&&value.schema_version===3)return managedDocumentValid(value);
  if(object(value)&&value.schema_version===2)return lifecycleDocumentValid(value);
  if (!fields(value,['schema_version','identity','revision','write_id','lock','topology_declared','nodes']) || value.schema_version !== 1 || !identityValid(value.identity) || !integer(value.revision,1,Number.MAX_SAFE_INTEGER) || !safe(value.write_id)) return false;
  if (!fields(value.lock,['state','run_id']) || !((value.lock.state === 'held' && safe(value.lock.run_id)) || (value.lock.state === 'idle' && value.lock.run_id === null))) return false;
  if (typeof value.topology_declared !== 'boolean' || !object(value.nodes)) return false;
  const nodes=Object.entries(value.nodes);
  if (nodes.length > 1000 || (value.topology_declared ? nodes.length === 0 : nodes.length !== 0)) return false;
  const operations=new Set<string>();const roles=new Map<string|null,number[]>();
  for(const [id,node] of nodes) {
    if (!safe(id) || !fields(node,['state_key','role','index','phase','operation_id']) || !roleValid(node.role) || !integer(node.index,0,999)) return false;
    if (id !== (node.role === null ? String(node.index) : `${node.role}-${node.index}`) || node.state_key !== `${value.identity.profile}/compute/nodes/${id}.tfstate`) return false;
    if (!['declared','running','ready','failed'].includes(node.phase)) return false;
    if (node.phase === 'declared') {if(node.operation_id !== null) return false;}
    else {
      if (!safe(node.operation_id) || operations.has(node.operation_id)) return false;
      operations.add(node.operation_id);
    }
    if (node.phase === 'running' && value.lock.state !== 'held') return false;
    roles.set(node.role,[...(roles.get(node.role) ?? []),node.index]);
  }
  if(roles.has(null) && roles.size !== 1) return false;
  return [...roles.values()].every(indices => indices.sort((a,b)=>a-b).every((index,position)=>index === position));
}
export function identityEqual(a: Map,b: Map) {
  return a.profile===b.profile && a.provider===b.provider && ['kind','bucket','region','endpoint'].every(key=>a.backend[key]===b.backend[key]);
}
function fail(message: string): never {throw new Error(message);}

/** Plan one journal CAS. A returned plan does not authorize provider dispatch. */
export function coordination(observation: unknown, identity: unknown, event: unknown) {
  if(object(event)&&typeof event.type==='string'&&event.type.startsWith('managed/'))return managedCoordination(observation,identity,event);
  if(object(event)&&typeof event.type==='string'&&event.type.startsWith('lifecycle/'))return lifecycle(observation,identity,event);
  if(!identityValid(identity)) fail('invalid coordination identity');
  if(!eventValid(event)) fail('invalid coordination event');
  if(!observationValid(observation)) fail('invalid coordination observation');
  if(observation.status === 'error') fail('coordination read failed');
  const present=observation.status === 'present';
  const document=observation.document;
  if(present && (!documentValid(document)||document.schema_version!==1)) fail('invalid coordination document');
  if(present && !identityEqual(document.identity,identity)) fail('coordination identity mismatch');
  if(event.target_etag !== (present ? observation.etag : null)) fail('stale coordination observation');
  if(present && event.write_id === document.write_id) fail('coordination write_id reused');
  if(present && document.revision === Number.MAX_SAFE_INTEGER) fail('coordination revision exhausted');
  if(!present) {
    if(event.type !== 'acquire') fail('coordination object absent');
    return {condition:{if_none_match:'*'},document:{schema_version:1,identity:structuredClone(identity),revision:1,write_id:event.write_id,lock:{state:'held',run_id:event.run_id},topology_declared:false,nodes:{}}};
  }
  const next:Map=structuredClone(document);
  if(event.type === 'acquire') {
    if(next.lock.state === 'held') fail('coordination lock held');
    next.lock={state:'held',run_id:event.run_id};
  } else {
    if(next.lock.state !== 'held') fail('coordination lock not held');
    if(next.lock.run_id !== event.run_id) fail('coordination owner mismatch');
    if(event.type === 'declare') {
      if(next.topology_declared) fail('coordination topology already declared');
      const expanded=expand(event.topology);
      const keys=state_keys(identity.profile,expanded.map(node=>node.node_id));
      next.nodes=Object.fromEntries(expanded.map(node=>[node.node_id,{state_key:keys.nodes[node.node_id],role:node.role,index:node.index,phase:'declared',operation_id:null}]));
      next.topology_declared=true;
    } else if(event.type === 'release') {
      if(Object.values(next.nodes).some((node:any)=>node.phase === 'running')) fail('coordination operations outstanding');
      next.lock={state:'idle',run_id:null};
    } else {
      if(!next.topology_declared) fail('coordination topology not declared');
      if(!Object.hasOwn(next.nodes,event.node_id)) fail('coordination node undeclared');
      const node=next.nodes[event.node_id];
      if(event.type === 'start') {
        if(node.phase !== 'declared') fail('coordination node not startable');
        if(Object.values(next.nodes).some((n:any)=>n.operation_id===event.operation_id)) fail('coordination operation_id reused');
        node.phase='running';node.operation_id=event.operation_id;
      } else {
        if(node.phase !== 'running') fail('coordination node not running');
        if(node.operation_id !== event.operation_id) fail('coordination operation mismatch');
        node.phase=event.type === 'complete' ? 'ready' : 'failed';
      }
    }
  }
  next.revision++;next.write_id=event.write_id;
  return {condition:{if_match:observation.etag},document:next};
}
