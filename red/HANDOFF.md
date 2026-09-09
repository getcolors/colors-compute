# Red contract and orchestration handoff

Implemented six pure operations in `src/index.ts`, a distributable copy of
`contracts/providers.json`, and JSONL driver `../scripts/contract-red.ts`.
Registry data is deeply frozen; a test rejects drift from the canonical file.
The manifest and Bun lockfile pin Red SDK commit
`db9bfe61282e2093f4098bf5a6ee0cd10added6e`.

## Cluster workflow adapter

`clusterWorkflow(requests, entryNodeId, nodeStep, downstream?)` is exported
from the main module and implemented in `src/workflow.ts`. It returns an actual
Red SDK Workflow using dynamic SDK fan-out and a shared join step. Execute with
`run(workflow, opts)` or embed using the SDK's `step` helper.

The common `nodeStep` is a Red StepFn: it receives its individual expanded
request at `colors-compute/request` and returns opts containing its normalized
node result at `colors-compute/params`. It reports failure by throwing or setting
`red/exit` above zero. The join writes `colors-compute/cluster`; optional
`downstream` receives that aggregate. A single request uses this same callback.
The constructor snapshots requests, and the SDK freezes branch inputs. SDK
failure propagation preserves branch records and skips join/downstream. An
incomplete successful result fails collection before downstream.

This adapter performs no cloud lifecycle or state I/O. A caller-supplied node
operation is still required. No package migration or provider execution is
claimed complete.

## Validation

Bun 1.3.13: 15 tests with 62 assertions pass. Coverage includes reversed branch
completion order, failed branch suppression of downstream, one-node operation,
missing results, request snapshots, provider mismatch, malformed topology,
registry immutability, remote-state refusals, and safe state identities.
`bun run typecheck` passes for source and tests using installed local tooling.
Earlier two-line JSONL smoke check returned expansion and unreadable-state
error output with no extra stdout.

The parent owns cross-color parity, root packaging, package migrations, registry
resource synchronization, and commits. No commits or pushes performed here.

## Rendering and backend planning

Added `render_template(value, inputs)` and `backend_plan(opts, state_key)` in
`src/rendering.ts`, exported from the main module for the JSONL driver. Rendering
preserves JSON scalar types and Terraform expressions, rejects malformed or
partial placeholders, leaves keys unchanged, and clones substituted objects.
Backend planning validates settings and state paths, emits native lockfile
configuration, and maps R2 credential names to partial backend options without
reading credentials or modifying the AWS environment. Actual protected
credential binding, backend initialization and locking remain runtime work.

Updated validation: 20 tests / 94 assertions pass; source and test typechecking
passes. No cloud operations, commits, or pushes were performed.

## Packaged provider plans

Added `provider_plan(provider, stage, inputs)` in `src/providers.ts`, exported
through the main module and automatically available to the JSONL driver. It
loads the generated packaged bundle, deep-freezes the template data, selects
providers/stages by own-property lookup, and renders detached documents using
the common typed renderer. No provider-specific runtime branching is added.

Validation now passes 23 tests / 106 assertions plus source/test typechecking.
New tests cover eight bundled providers, invalid/prototype selections, typed
Vultr output, immutable source templates, and a subprocess that imports a copy
of the distributable src/resources/package.json outside the repository while
reusing the already-installed pinned SDK dependencies. Generated bundles were
not modified; the parent owns their synchronization. This remains a render
API, not a state or cloud lifecycle implementation.
