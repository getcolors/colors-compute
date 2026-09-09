import {provider_power} from '../red/src/power.ts';
import * as contract from '../red/src/index.ts';
import {controller_artifact} from '../red/src/controller.ts';
import {writeFileSync} from 'node:fs';
import {journalGet,journalPut} from '../red/src/journal.ts';
import {readState} from '../red/src/backend.ts';
async function read_state_case(opts: any, key: string, environment: any, responses: any[]) {
  let index = 0;
  return readState(opts, key, environment, async () => responses[index++]);
}
async function journal_case(opts:any, environment:any, response:any, body:any, intent:any=null) {
  const runner=async (command:string[]) => {
    if(command[2]==='get-object') writeFileSync(command[7],typeof body === "string" ? body : Buffer.from(body.bytes_base64,"base64"));
    return response;
  };
  return intent === null ? journalGet(opts,environment,runner) : journalPut(opts,intent,environment,runner);
}
async function provider_power_case(opts:any,action:string,id:string,env:any,responses:any[]){let index=0;return provider_power(opts,action,id,env,{http:()=>responses[index++],runner:()=>responses[index++],sleep:()=>{}});}
const input = await Bun.stdin.text();
for (const line of input.split('\n').filter(line => line.trim())) {
  try {
    const {op, args} = JSON.parse(line);
    const fn = op === "provider_power_case" ? provider_power_case : op === "controller_artifact" ? controller_artifact : op === "journal_case" ? journal_case : op === "read_state_case" ? read_state_case : (contract as Record<string, unknown>)[op];
    if (typeof fn !== 'function') throw new Error(`unknown operation: ${op}`);
    console.log(JSON.stringify(await fn(...args)));
  } catch (error) {
    console.log(JSON.stringify({error: error instanceof Error ? error.message : String(error)}));
  }
}
