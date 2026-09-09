import {chmodSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {backend_plan} from './rendering.ts';
export interface BackendCommandResult {exit: number; out: string; err: string}
export type BackendRunner = (args: string[], options: {cwd: string; env: Record<string, string>; timeoutMs: number}) => Promise<BackendCommandResult>;
export type StateRead = {status: 'present'; params: Record<string, unknown>; outputs?:Record<string,unknown>; state_empty?:boolean} | {status: 'error'};
const object = (value: unknown): value is Record<string, any> => value !== null && typeof value === 'object' && !Array.isArray(value);
const missing = (value: unknown) => typeof value !== 'string' || !value.trim() || value.trim().toUpperCase() === 'REPLACE_ME';
export const executeBackendCommand: BackendRunner = async (args, options) => {
  const child = Bun.spawn(args, {cwd: options.cwd, env: options.env, detached:process.platform !== 'win32', stdin:'ignore',stdout:'pipe',stderr:'pipe'});
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    try {
      if (process.platform !== 'win32') process.kill(-child.pid,'SIGKILL');
      else child.kill(9);
    } catch {
      try {child.kill(9);} catch {}
    }
  }, options.timeoutMs);
  try {
    const [out,err,exit] = await Promise.all([new Response(child.stdout).text(),new Response(child.stderr).text(),child.exited]);
    if (timedOut) throw new Error('backend command timed out');
    return {exit,out,err};
  } finally {clearTimeout(timer);}
};

export function parseStateEnvelope(output:string):{document:Record<string,any>;params:Record<string,any>} {
  const document:unknown=JSON.parse(output);
  if(!object(document)||document.version!==4||!Number.isSafeInteger(document.serial)||document.serial<0||typeof document.lineage!=='string'||!document.lineage.trim()||!object(document.outputs)||!Array.isArray(document.resources))throw new Error('invalid state');
  if(!Object.hasOwn(document.outputs,'params'))return {document,params:{}};
  const params=document.outputs.params;if(!object(params)||!object(params.value))throw new Error('invalid params');
  return {document,params:params.value};
}
export function stateOutputs(output:string):Record<string,unknown> {
  const {document}=parseStateEnvelope(output);
  return Object.fromEntries(Object.entries(document.outputs).map(([name,entry])=>{
    if(!object(entry)||!Object.hasOwn(entry,'value')||(Object.hasOwn(entry,'sensitive')&&entry.sensitive!==false))throw new Error('invalid outputs');
    return [name,entry.value];
  }));
}

/** Read an existing remote state using an isolated, protected backend context.
 * This operation never interprets failure as absence and never mutates compute.
 */
export async function readState(
  opts: Record<string, any>,
  stateKey: string,
  environment: Record<string, string | undefined> = process.env,
  runner: BackendRunner = executeBackendCommand,
  includeOutputs = false,
): Promise<StateRead> {
  let directory: string | undefined;
  try {
    const plan = backend_plan(opts, stateKey);
    const settings: Record<string,string> = {};
    for (const [variable, option] of Object.entries(plan.credential_bindings)) {
      const value = environment[variable];
      if (missing(value)) return {status:'error'};
      settings[option] = value!;
    }
    directory = mkdtempSync(join(tmpdir(),'colors-compute-state-'));
    chmodSync(directory,0o700);
    const configPath = join(directory,'credentials.tfbackend.json');
    writeFileSync(join(directory,'backend.tf.json'), JSON.stringify(plan.config),{mode:0o600,flag:'wx'});
    writeFileSync(configPath,JSON.stringify(settings),{mode:0o600,flag:'wx'});
    const env: Record<string,string> = {};
    for (const [key,value] of Object.entries(environment)) {
      if (value !== undefined && !/^(TF_|TOFU_|COLORS_PAR_)/.test(key)) env[key] = value;
    }
    if (opts['provider-backend'] === 'r2') {
      delete env.AWS_PROFILE;
      delete env.AWS_DEFAULT_PROFILE;
    }
    Object.assign(env,{TF_IN_AUTOMATION:'1',TF_INPUT:'0',TF_WORKSPACE:'default',TF_DATA_DIR:join(directory,'.terraform')});
    const options = {cwd:directory,env,timeoutMs:120_000};
    const init = await runner(['tofu','init','-input=false','-no-color','-reconfigure',`-backend-config=${configPath}`],options);
    if (init.exit !== 0) return {status:'error'};
    const state = await runner(['tofu','state','pull'],options);
    if (state.exit !== 0 || !state.out.trim()) return {status:'error'};
    const {params}=parseStateEnvelope(state.out);
    const outputs=includeOutputs?stateOutputs(state.out):undefined;
    const serialized = JSON.stringify(includeOutputs?outputs:params);
    if (Object.values(settings).some(value => serialized.includes(value) || serialized.includes(JSON.stringify(value).slice(1,-1)))) return {status:'error'};
    return {status:'present',params,...(includeOutputs?{outputs,state_empty:parseStateEnvelope(state.out).document.resources.length===0&&Object.keys(outputs!).length===0}:{})};
  } catch(error) {
    if(error instanceof Error&&error.name==='AbortError')throw error;
    return {status:'error'};
  } finally {
    if (directory) {
      try {rmSync(directory,{recursive:true,force:true});}
      catch {return {status:'error'};}
    }
  }
}
