import {workflow, type Opts, type StepFn, type Workflow} from 'red/workflow';
import {collect, state_keys} from './index.ts';

/** Build a Colors fork/join around an injected single-node operation.
 * nodeStep receives colors-compute/request and returns colors-compute/params.
 * It must throw or return red/exit > 0 on failure. No provisioning is supplied
 * by this adapter; the caller supplies the common single-node lifecycle step.
 */
export function clusterWorkflow(
  requests: Opts[],
  entryNodeId: string,
  nodeStep: StepFn,
  downstream?: StepFn,
): Workflow {
  if (!requests.length) throw new Error('no nodes requested');
  const ids = new Set<string>();
  for (const request of requests) {
    if (ids.has(request.node_id)) throw new Error(`duplicate requested node: ${request.node_id}`);
    ids.add(request.node_id);
  }
  if (!ids.has(entryNodeId)) throw new Error(`unknown entry node: ${entryNodeId}`);
  state_keys('validation', requests.map(request => request.node_id));
  // Snapshot configuration so caller mutation cannot change a constructed DAG.
  const declared = structuredClone(requests);
  return workflow({
    start: 'colors-compute/fan-out',
    wireFn: (step) => {
      switch (step) {
        case 'colors-compute/fan-out':
          return [opts => ({...opts}), 'colors-compute/node'];
        case 'colors-compute/node':
          return [nodeStep, 'colors-compute/join'];
        case 'colors-compute/join':
          return [opts => {
            // A one-node route has no fork, so the SDK supplies its result
            // directly rather than creating red/branches.
            const branches: Opts[] = declared.length === 1 ? [opts] : opts['red/branches'];
            const results = branches.map(branch => branch['colors-compute/params'])
              .filter(result => result !== undefined && result !== null);
            return {...opts, 'colors-compute/cluster': collect(declared, results, entryNodeId)};
          }, ...(downstream ? ['colors-compute/downstream'] : [])];
        case 'colors-compute/downstream':
          return downstream ? [downstream] : undefined;
      }
    },
    nextFn: (step, next, opts) => {
      if ((opts['red/exit'] ?? 0) > 0) return [];
      if (step === 'colors-compute/fan-out') {
        return declared.map(request => ['colors-compute/node', {
          ...opts, 'colors-compute/request': structuredClone(request),
        }] as const);
      }
      return (next ?? []).map(name => [name, opts] as const);
    },
  });
}
