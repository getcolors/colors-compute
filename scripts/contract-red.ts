import * as contract from '../red/src/index.ts';
import {redact} from '../red/src/diagnostics.ts';
const input = await Bun.stdin.text();
for (const line of input.split('\n').filter(line => line.trim())) {
  try {
    const {op, args} = JSON.parse(line);
    const fn = op === 'sanitize_error' ? ((value: any, opts: any, environment: any) => redact(value, opts, environment) ?? null) : op === 'node_runtime_error' ? ((opts: any, request: any, operation: string) => contract.compute_node(opts, request, operation, {})) : op === 'node_plan_valid' ? ((opts: any, request: any) => {try {contract.node_plan(opts, request); return true;} catch {return false;}}) : (contract as Record<string, unknown>)[op];
    if (typeof fn !== 'function') throw new Error(`unknown operation: ${op}`);
    console.log(JSON.stringify(await fn(...args)));
  } catch (error) {
    console.log(JSON.stringify({error: error instanceof Error ? error.message : String(error)}));
  }
}
