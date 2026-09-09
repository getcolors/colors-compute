/** Pure contract foundation. This module does not execute infrastructure. */
import registryData from '../resources/providers.json';
function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}
export const registry = deepFreeze(registryData);
type Map = Record<string, any>;
const safe = (x: unknown): x is string => typeof x === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/.exec(x)?.[0] === x;
const missing = (x: unknown) => x == null || (typeof x === 'string' && (!x.trim() || x.trim().toUpperCase() === 'REPLACE_ME'));
const nonblank = (x: unknown) => typeof x === 'string' && !!x.trim();
function fail(message: string): never { throw new Error(message); }
const entries = registry as unknown as {compute: Record<string, Map>; backend: Record<string, Map>};
function selections(opts: Map): string[] {
  const errors: string[] = [];
  if (typeof opts['provider-compute'] !== 'string' || !Object.hasOwn(entries.compute, opts['provider-compute'])) errors.push(':provider-compute must be one of ' + Object.keys(entries.compute).sort().join(', '));
  if (typeof opts['provider-backend'] !== 'string' || !Object.hasOwn(entries.backend, opts['provider-backend'])) errors.push(':provider-backend must be one of ' + Object.keys(entries.backend).sort().join(', '));
  return errors;
}
function selected(opts: Map): Map[] {
  return [['compute', 'provider-compute'], ['backend', 'provider-backend']].flatMap(([kind, option]) => {
    const group = entries[kind as keyof typeof entries];
    const name = opts[option];
    return typeof name === 'string' && Object.hasOwn(group, name) ? [group[name]] : [];
  });
}
export function validate(opts: Map): string[] {
  const errors = selections(opts);
  if (missing(opts.profile)) errors.push(':profile is required');
  else if (!safe(opts.profile)) errors.push(':profile must be a safe identifier');
  const required = new Set<string>(selected(opts).flatMap(e => e.required));
  for (const key of [...required].sort()) if (missing(opts[key])) errors.push(`:${key} is required`);
  for (const entry of selected(opts)) for (const alternatives of entry['required-one-of'] ?? []) {
    if (!alternatives.some((group: string[]) => group.every(key => !missing(opts[key]))))
      errors.push('one of ' + alternatives.map((group: string[]) => group.map(key => ':' + key).join(' and ')).join(' or ') + ' is required');
  }
  return errors;
}
export function credential_requirements(opts: Map): string[] {
  const errors = selections(opts);
  if (errors.length) fail(errors.join('; '));
  return [...new Set<string>(selected(opts).flatMap(e => e.secrets.map((key: string) => 'COLORS_PAR_' + key.toUpperCase().replaceAll('-', '_'))))].sort();
}
export function compute_credential_errors(opts: Map, environment: Map): string[] {
  const provider=opts['provider-compute'];
  if(typeof provider!=='string'||!Object.hasOwn(entries.compute,provider))fail('invalid compute provider');
  return entries.compute[provider].secrets.map((key:string)=>'COLORS_PAR_'+key.toUpperCase().replaceAll('-','_')).sort()
    .filter((key:string)=>typeof environment[key]!=='string'||missing(environment[key]))
    .map((key:string)=>'required credential is not set: '+key);
}
export function state_keys(profile: string, node_ids: string[]) {
  if (!safe(profile)) fail(':profile must be a safe identifier');
  const nodes: Record<string, string> = Object.create(null);
  for (const id of node_ids) {
    if (!safe(id)) fail(`invalid node_id: ${id}`);
    if (Object.hasOwn(nodes, id)) fail(`duplicate node_id: ${id}`);
    nodes[id] = `${profile}/compute/nodes/${id}.tfstate`;
  }
  return {shared: `${profile}/compute/shared.tfstate`, nodes};
}
export function expand(topology: Map[]) {
  if (!Array.isArray(topology) || !topology.length) fail('topology must declare at least one role');
  const roles = new Set<string | null>();
  const nodes: Map[] = [];
  for (const declaration of topology) {
    const role = declaration.role ?? null;
    if (role !== null && (typeof role !== 'string' || /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.exec(role)?.[0] !== role)) fail('invalid role');
    if (roles.has(role)) fail('duplicate role');
    roles.add(role);
    if (role === null && topology.length !== 1) fail('a null role must be the only role');
    const count = declaration.count === undefined ? 1 : declaration.count;
    if (typeof count !== 'number' || !Number.isSafeInteger(count) || count < 1) fail('count must be a positive integer');
    for (let index = 0; index < count; index++) {
      const node_id = role === null ? `${index}` : `${role}-${index}`;
      if (!safe(node_id)) fail(`invalid node_id: ${node_id}`);
      nodes.push({node_id, role, index});
    }
  }
  return nodes;
}
export function collect(requests: Map[], results: Map[], entry_node_id: string) {
  if (!requests.length) fail('no nodes requested');
  const ids = new Set<string>();
  for (const request of requests) {
    if (ids.has(request.node_id)) fail(`duplicate requested node: ${request.node_id}`);
    ids.add(request.node_id);
  }
  if (!ids.has(entry_node_id)) fail(`unknown entry node: ${entry_node_id}`);
  const byId = new Map<string, Map>();
  for (const result of results) {
    if (!ids.has(result.node_id)) fail(`undeclared node: ${result.node_id ?? null}`);
    if (byId.has(result.node_id)) fail(`duplicate node: ${result.node_id}`);
    byId.set(result.node_id, result);
  }
  let provider: string | undefined;
  const nodes: Map[] = requests.map(request => {
    const id = request.node_id;
    const result = byId.get(id);
    if (!result) fail(`missing node: ${id}`);
    const fields = ['provider', 'name', 'ip', 'user', 'sudoer'];
    if (request.private === true) fields.push('vpc_ip');
    for (const field of fields) if (!nonblank(result[field])) fail(`incomplete node ${id}: ${field}`);
    provider ??= result.provider;
    if (result.provider !== provider || result.provider !== (request.provider ?? provider)) fail(`provider mismatch: ${id}`);
    return {...result, role: request.role ?? null, index: request.index ?? null};
  });
  return {provider, entry_node_id, nodes};
}
export function state_decision(read: Map, selected: string) {
  switch (read.status) {
    case 'absent': return {action: 'create'};
    case 'error': return fail('could not read compute state; refusing mutation');
    case 'present': {
      const recorded = read.params?.provider;
      if (typeof recorded !== 'string' || missing(recorded)) fail('legacy state requires migration');
      if (recorded !== selected) fail(`state holds a ${recorded} machine; set provider-compute back to ${recorded} and delete first`);
      return {action: 'reuse'};
    }
    default: return fail('invalid state read status');
  }
}

export {clusterWorkflow} from './workflow.ts';
export {render_template, backend_plan} from './rendering.ts';
export {provider_plan} from './providers.ts';
export {readState, type BackendRunner, type StateRead} from './backend.ts';
export {coordination} from './coordination.ts';
export {journalGet,journalPut,type JournalGetResult,type JournalPutResult} from './journal.ts';
export {Coordinator,type CoordinatorOptions} from './coordinator.ts';
export {provider_request} from './provider-request.ts';
export {prepareKeypair,cleanupKeypair,SSHError} from './ssh.ts';
export {statePresence,convergeState} from './execution.ts';
export {deployment_requests} from './deployment-request.ts';
export {key_request} from './key-request.ts';
export {orchestrate} from './orchestration.ts';
export {source_cidrs} from './deployment-request.ts';
export {plan_deployment} from './planning.ts';
export {read_deployment} from './inspection.ts';
export {registrationPreflight} from './registration.ts';
export {mode as keyMode} from './ssh.ts';
export {validate_deployment} from './planning.ts';
export {endpoint_agent} from './endpoint.ts';
