import {localDirectory} from './local.ts';
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
  if (Object.hasOwn(opts,"compute-require-existing-state") && typeof opts["compute-require-existing-state"] !== "boolean") errors.push(":compute-require-existing-state must be a boolean");
  const required = new Set<string>(selected(opts).flatMap(e => e.required));
  for (const key of [...required].sort()) if (missing(opts[key])) errors.push(`:${key} is required`);
  for (const entry of selected(opts)) for (const alternatives of entry['required-one-of'] ?? []) {
    if (!alternatives.some((group: string[]) => group.every(key => !missing(opts[key]))))
      errors.push('one of ' + alternatives.map((group: string[]) => group.map(key => ':' + key).join(' and ')).join(' or ') + ' is required');
  }
  if (opts['provider-backend']==='local') {
    try {localDirectory(opts);} catch(error) {errors.push((error as Error).message);}
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
export {render_template,backend_plan} from './rendering.ts';
export {provider_plan} from './providers.ts';
export {provider_request} from './provider-request.ts';
export {endpoint_agent} from './endpoint.ts';
export {controller_artifact} from './controller.ts';
export {node_layout,node_plan,build_node,compute_node,registration_plan,build_registration,compute_registration} from './node.ts';

export {ssh_plan,ssh_resource,start_agent} from "./ssh.ts";
