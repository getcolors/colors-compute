import {existsSync,statSync} from 'node:fs';
import {resolve,delimiter} from 'node:path';
import {executable} from './diagnostics.ts';
export interface BackendCommandResult {exit: number; out: string; err: string; command_reason?: 'executable_not_found' | 'process_start_failed' | 'timeout'}
export type BackendRunner = (args: string[], options: {cwd: string; env: Record<string, string>; timeoutMs: number}) => Promise<BackendCommandResult>;
export type StateRead = {status: 'present'; params: Record<string, unknown>; outputs?:Record<string,unknown>; state_empty?:boolean} | {status: 'error'};
const object = (value: unknown): value is Record<string, any> => value !== null && typeof value === 'object' && !Array.isArray(value);
const missing = (value: unknown) => typeof value !== 'string' || !value.trim() || value.trim().toUpperCase() === 'REPLACE_ME';
export const executeBackendCommand: BackendRunner = async (args, options) => {
  const failed = (command_reason: BackendCommandResult['command_reason']): BackendCommandResult => ({exit:-1,out:'',err:'',command_reason});
  try {if (!statSync(options.cwd).isDirectory()) return failed('process_start_failed');} catch {return failed('process_start_failed');}
  const resolved = executable(args[0],options.cwd,options.env);
  if (!resolved) {
    const candidates=args[0].includes('/')?[resolve(options.cwd,args[0])]:options.env.PATH===undefined?[]:options.env.PATH.split(delimiter).map(part=>resolve(options.cwd,part,args[0]));
    return failed(candidates.some(path=>existsSync(path))?'process_start_failed':'executable_not_found');
  }
  let child;
  try {child = Bun.spawn([resolved,...args.slice(1)], {cwd: options.cwd, env: options.env, detached:process.platform !== 'win32', stdin:'ignore',stdout:'pipe',stderr:'pipe'});} catch {return failed('process_start_failed');}
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
    if (timedOut) return failed('timeout');
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
