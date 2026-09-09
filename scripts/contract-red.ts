import * as contract from '../red/src/index.ts';
import {readState} from '../red/src/backend.ts';
async function read_state_case(opts: any, key: string, environment: any, responses: any[]) {
  let index = 0;
  return readState(opts, key, environment, async () => responses[index++]);
}
const input = await Bun.stdin.text();
for (const line of input.split('\n').filter(line => line.trim())) {
  try {
    const {op, args} = JSON.parse(line);
    const fn = op === "read_state_case" ? read_state_case : (contract as Record<string, unknown>)[op];
    if (typeof fn !== 'function') throw new Error(`unknown operation: ${op}`);
    console.log(JSON.stringify(await fn(...args)));
  } catch (error) {
    console.log(JSON.stringify({error: error instanceof Error ? error.message : String(error)}));
  }
}
