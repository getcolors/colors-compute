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

## Protected read-only backend sessions

Added async `readState(opts, stateKey, environment=process.env, runner?)`, with
exported BackendRunner/StateRead types. It implements contracts/runtime.md:
exact sanitized environment, 120-second native runner timeout, private 0700
session and 0600 backend/partial credentials files, fixed init then state pull,
strict state envelope checks, generic present/error results and cleanup. R2
credentials never override ambient AWS auth, and bound values cannot appear in
returned params. No missing-state inference or cloud mutation API is added.

Seven backend tests / 59 assertions pass, including an actual fake-tofu child
that verifies exact environment filtering. Source/test typechecking passes.
The runtime security review records tested SDK risks: negative exit acceptance,
inherited CLI/log settings, raw diagnostic propagation and generic file modes.
No SDK changes were made. Backend absence/ownership/coordination and live locks
remain unimplemented; callers must not log potentially sensitive state params.

The native runner now creates a separate POSIX process group and kills the whole
group on timeout. An actual shell exiting while its background child holds
stdout/stderr verifies termination in about 100 ms, rather than hanging until
the child's 30-second sleep completes. Backend coverage is now 8 tests / 61
assertions; typechecking still passes. Windows uses direct-child termination
and does not have the POSIX descendant-cleanup guarantee.

Red's injected runner signature is `(args, {cwd, env, timeoutMs}) => Promise<
{exit, out, err}>`; it receives `timeoutMs: 120000`. The internal module exports
`executeBackendCommand` for direct timeout testing; the root module exports
only the readState API and runner/result types.

## Minimal pure coordination reducer

Added `coordination(observation, identity, event)` from contracts/coordination.md.
It validates strict allowlists and matching identity/ETag, plans conditional
acquire/declare/start/complete/fail/release writes, derives stable node state
keys, preserves attempted node records, refuses release with running attempts,
and never treats age or matching owner ID as lock takeover permission. Returned
CAS plans do not authorize dispatch until a transport confirms the exact write.
No transport, retries, scale-down, deletion, key ownership or recovery added.

The contract and initial 39 shared fixtures were authored in this subtask;
parent audit cases now expand the common suite. Red passes 55 coordination tests
(54 fixture cases plus output detachment), 110 assertions, and typechecking.
Input immutability is checked in every shared case. Mathematical integer values
such as 1.0 are accepted consistently with the revised contract.

The R2 backend-only environment now removes AWS_PROFILE and AWS_DEFAULT_PROFILE
to avoid unrelated profile resolution blocking explicit R2 credentials. S3
preserves profile selection; ambient AWS key values and the parent environment
remain unchanged. Backend tests pass with 65 assertions after this correction.

Full-match validation was corrected for existing profile/node/role/state-key and
whole-template-placeholder regexes: JavaScript `$` can otherwise accept a final
line terminator. New regression tests reject LF, CR, CRLF, Unicode line separator
and paragraph separator suffixes. Focused contract/rendering tests pass (17
cases, 97 assertions) and typechecking passes. Coordination already used exact
matched-string comparison and needed no corresponding change.

## Conditional journal object transport

Added `journalGet(opts, environment?, runner?)` and
`journalPut(opts, intent, environment?, runner?)`, using the existing native
120-second exact-environment runner. Operations invoke only AWS CLI GetObject
or conditional PutObject for the derived profile coordination key. Put validates
strict reducer document/identity/condition schemas before execution. Get returns
a bounded untrusted JSON object, which must pass reducer validation before use.

R2 uses private 0600 credentials/config files under a fresh 0700 directory,
removes inherited AWS variables except AWS_CA_BUNDLE, and supplies only the
backend's access/secret pair to its CLI child. S3 retains the ambient credential
chain. Both remove COLORS_PAR variables and disable pager, prompts and retries.
Documents are limited to 2 MiB; diagnostics and bound credential echoes cannot
escape through results. Cleanup runs on success, failure and thrown execution.
Only exact service-error prefixes on the expected operation classify absence or
conditional conflict. Unknown or ambiguous failures return error with no retry.

Eight new journal tests / 91 assertions and source/test typechecking pass.
Tests inspect permissions, credential placement, AWS isolation, missing-key vs
other errors, conditional conflicts, malformed/oversized documents, strict
write schemas and cleanup. Actual AWS CLI loopback signing/CAS evidence is
parent-owned work; these fake-runner tests do not establish live S3/R2 behavior.
No cloud operations, commits or pushes were performed by this subtask.

Journal runner AbortError cancellation is propagated after cleanup; ordinary
execution errors remain generic. A cancellation regression test brings journal
coverage to 9 tests / 93 assertions, all passing with typechecking.

AWS CLI 2.35.11 service-error formatting is now supported: exact optional
`aws: [ERROR]: ` prefix and optional numeric `(reached max retries: N)` suffix.
Legacy formatting remains supported; wrong operations, misleading substrings
and unknown suffixes still fail closed. Journal tests now pass 10 cases / 99
assertions, and typechecking passes. Parent owns the actual loopback probe.

Journal body decoding now uses a fatal UTF-8 decoder and retains a leading BOM
so JSON parsing refuses it. Tests reject malformed UTF-8, UTF-16 and UTF-8 BOM
bodies while proving cleanup. Verified TextDecoder ignoreBOM:true retains U+FEFF.
Focused journal tests pass: 11 cases / 106 assertions. Unchanged broad checks
were not rerun for this isolated decoding correction.
