import {chmodSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {backend_plan} from './rendering.ts';
export interface BackendCommandResult {exit: number; out: string; err: string}
export type BackendRunner = (args: string[], options: {cwd: string; env: Record<string, string>; timeoutMs: number}) => Promise<BackendCommandResult>;
export type StateRead = {status: 'present'; params: Record<string, unknown>} | {status: 'error'};
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

/** Read an existing remote state using an isolated, protected backend context.
 * This operation never interprets failure as absence and never mutates compute.
 */
export async function readState(
  opts: Record<string, any>,
  stateKey: string,
  environment: Record<string, string | undefined> = process.env,
  runner: BackendRunner = executeBackendCommand,
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
    const data: unknown = JSON.parse(state.out);
    if (!object(data) || data.version !== 4 || !Number.isSafeInteger(data.serial) || data.serial < 0 ||
      typeof data.lineage !== 'string' || !data.lineage.trim() || !object(data.outputs) || !Array.isArray(data.resources)) return {status:'error'};
    if (!Object.hasOwn(data.outputs,'params')) return {status:'present',params:{}};
    const output = data.outputs.params;
    if (!object(output) || !object(output.value)) return {status:'error'};
    const serialized = JSON.stringify(output.value);
    if (Object.values(settings).some(value => serialized.includes(value) || serialized.includes(JSON.stringify(value).slice(1,-1)))) return {status:'error'};
    return {status:'present',params:output.value};
  } catch {
    return {status:'error'};
  } finally {
    if (directory) {
      try {rmSync(directory,{recursive:true,force:true});}
      catch {return {status:'error'};}
    }
  }
}
