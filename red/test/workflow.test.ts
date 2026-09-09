import {expect, test} from 'bun:test';
import {run, type StepFn} from 'red/workflow';
import {clusterWorkflow, expand} from '../src/index.ts';
const params = (request: Record<string, any>) => ({node_id: request.node_id, provider:'vultr', name:request.node_id, ip:'192.0.2.1', user:'root', sudoer:'root', metadata:{id:request.node_id}});
const node: StepFn = opts => ({...opts, 'colors-compute/params':params(opts['colors-compute/request'])});
test('previous results cannot satisfy a new node operation', async () => {
  const old = params({node_id:'0'});
  const result = await run(clusterWorkflow(expand([{}]), '0', opts => opts), {
    'colors-compute/params': old,
    'colors-compute/cluster': {provider:'vultr',nodes:[old]},
    'red/branches': [{'colors-compute/params':old}],
  });
  expect(result['red/exit']).toBeGreaterThan(0);
  expect(result['red/err']).toBe('missing node: 0');
  expect(result['colors-compute/cluster']).toBeUndefined();
});
test('SDK fan-out joins reversed completion once in declared order', async () => {
  const requests = expand([{role:'broker',count:3}]);
  const completed: string[] = [];
  let downstream = 0;
  const result = await run(clusterWorkflow(requests, 'broker-0', async opts => {
    const request = opts['colors-compute/request'];
    await Bun.sleep((3-request.index)*10);
    completed.push(request.node_id);
    expect(Object.isFrozen(request)).toBe(true);
    return node(opts);
  }, opts => { downstream++; return opts; }), {marker:'preserved'});
  expect(result['red/exit']).toBe(0);
  expect(completed).toEqual(['broker-2','broker-1','broker-0']);
  expect(result['colors-compute/cluster'].nodes.map((n: any) => n.node_id)).toEqual(['broker-0','broker-1','broker-2']);
  expect(result['colors-compute/cluster'].nodes[0].metadata).toEqual({id:'broker-0'});
  expect(result.marker).toBe('preserved');
  expect(downstream).toBe(1);
});
test('failed branch preserves branch records and prevents join/downstream', async () => {
  let downstream = false;
  const result = await run(clusterWorkflow(expand([{count:2}]), '0', opts => {
    if (opts['colors-compute/request'].node_id === '1') throw new Error('node unavailable');
    return node(opts);
  }, opts => {downstream = true; return opts;}), {});
  expect(result['red/exit']).toBeGreaterThan(0);
  expect(result['red/branches']).toHaveLength(2);
  expect(result['colors-compute/cluster']).toBeUndefined();
  expect(downstream).toBe(false);
});
test('single-node path uses identical callback and still collects', async () => {
  const result = await run(clusterWorkflow(expand([{}]), '0', node), {});
  expect(result['red/exit']).toBe(0);
  expect(result['colors-compute/cluster'].nodes[0].node_id).toBe('0');
});
test('incomplete successful return fails join before downstream', async () => {
  let downstream = false;
  const result = await run(clusterWorkflow(expand([{}]), '0', opts => opts,
    opts => { downstream = true; return opts; }), {});
  expect(result['red/exit']).toBeGreaterThan(0);
  expect(result['red/err']).toContain('missing node: 0');
  expect(downstream).toBe(false);
});
test('constructor refuses invalid identities and snapshots requests', async () => {
  expect(() => clusterWorkflow([], '0', node)).toThrow('no nodes requested');
  expect(() => clusterWorkflow([{node_id:'0'},{node_id:'0'}], '0', node)).toThrow('duplicate requested node: 0');
  expect(() => clusterWorkflow([{node_id:'0'}], '1', node)).toThrow('unknown entry node: 1');
  const requests = expand([{}]);
  const wf = clusterWorkflow(requests, '0', node);
  requests[0].node_id = 'changed';
  const result = await run(wf, {});
  expect(result['colors-compute/cluster'].nodes[0].node_id).toBe('0');
});
test('negative node exit fails the fork before join or downstream',async()=>{
  let downstream=false;
  const result=await run(clusterWorkflow(expand([{count:2}]),'0',async opts=>({...await node(opts),'red/exit':opts['colors-compute/request'].node_id==='0'?-1:0}),opts=>{downstream=true;return opts;}),{});
  expect(result['red/exit']).toBeGreaterThan(0);
  expect(result['colors-compute/cluster']).toBeUndefined();
  expect(downstream).toBe(false);
});
