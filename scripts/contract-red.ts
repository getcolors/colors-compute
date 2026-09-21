import * as contract from '../red/src/index.ts';
const input = await Bun.stdin.text();
for (const line of input.split('\n').filter(line => line.trim())) {
  try {
    const {op, args} = JSON.parse(line);
    const fn = op === 'node_plan_valid' ? ((opts: any, request: any) => {try {contract.node_plan(opts, request); return true;} catch {return false;}}) : (contract as Record<string, unknown>)[op];
    if (typeof fn !== 'function') throw new Error(`unknown operation: ${op}`);
    console.log(JSON.stringify(await fn(...args)));
  } catch (error) {
    console.log(JSON.stringify({error: error instanceof Error ? error.message : String(error)}));
  }
}
