# Deployment coordination and durable ownership proposal

Status: design only, assessed 2026-09-09. No object-storage writes, credentials,
live state reads or cloud operations were performed. None of the mechanisms
below is implemented by the current pure planning API.

## Recommendation

Use one conditional-write JSON object per deployment for both deployment lock
ownership and the durable node journal. Keep the lock non-expiring. Serialize
all journal updates through the orchestration coordinator while allowing node
OpenTofu operations to run concurrently. Retain OpenTofu's native lockfile for
each individual state. Do not implement automatic time-based lock takeover:
cloud APIs do not enforce a coordinator fencing token, so a paused old process
could resume after a lease expiry and mutate resources concurrently.

Proposed new reserved key: `<profile>/compute/coordination.json`. Existing state
keys remain `<profile>/compute/shared.tfstate` and
`<profile>/compute/nodes/<node_id>.tfstate`. The coordination object must not be
an OpenTofu-managed resource and must not be stored only inside shared tofu
state: it needs to survive shared-resource destruction and final key cleanup.

This design requires a small authenticated S3 object transport in the library.
The current Colors SDK supplies workflow and subprocess execution, but no
cross-state distributed lock/journal. Do not claim that setting
`use_lockfile: true`, invoking `tofu state pull`, or surrounding individual
node steps with SDK advice supplies this coordination.

## Evidence and feasibility

S3 supports conditional PutObject with `If-None-Match: *` for creation and
`If-Match: <ETag>` for replacement. Failed preconditions do not authorize
retrying an unconditional write. See [AWS conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).

Cloudflare's official S3 compatibility table lists both conditions for
PutObject, and its consistency documentation describes strongly consistent
object reads/writes. This makes the same protocol a candidate for R2; actual
endpoint concurrency tests are still required. Sources: [R2 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
and [R2 consistency](https://developers.cloudflare.com/r2/reference/consistency/).
The Cloudflare pages rejected direct retrieval in this session, so the current
official [API documentation source](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/docs/r2/api/s3/api.mdx)
and [consistency source](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/docs/r2/reference/consistency.mdx)
were inspected instead. S3-compatible branding alone is not sufficient evidence.

OpenTofu's S3 implementation uses a conditional lock-file PutObject with
`IfNoneMatch: "*"`; that protects the particular state, not an entire deployment.
See [OpenTofu S3 client source](https://github.com/opentofu/opentofu/blob/main/internal/backend/remote-state/s3/client.go).
The inspected `main` source is evidence for mechanism, not a compatibility
assertion for every installed release. Pin and test the actual runtime version.

Local evidence: `green/src/green/tofu.clj` runs init then apply/destroy and
merges outputs; its calls do not maintain a deployment-wide lock.
`green/src/green/process.clj` has process-tree timeout support, but tofu's current
runner uses shell/sh rather than that timeout runner. Equivalent Red/Blue tofu
entry points also require lifecycle review. The library's
`green/src/clj/io/github/getcolors/compute_workflow.clj` supplies node fan-out
and a collecting join, not journal persistence or cancellation ownership.
The revised workspace cluster standard explicitly requires attempted-node
records before dispatch and lock coverage through final cleanup.

## Object schema

Use canonical JSON, a bounded document size, and an explicit schema version.
Reject unknown incompatible versions before mutation. Proposed shape:

```json
{
  "schema_version": 1,
  "deployment_id": "random-stable-deployment-uuid",
  "profile": "example",
  "backend_identity": {"kind": "r2", "bucket": "state", "endpoint": "https://example.invalid", "region": "auto"},
  "revision": 1,
  "write_id": "unique-per-CAS-write-uuid",
  "status": "active",
  "lock": {"state": "held", "run_id": "unique-run-uuid", "operation": "create", "owner_label": "operator-host", "acquired_at": "UTC timestamp"},
  "provider": "vultr",
  "shared_state_key": "example/compute/shared.tfstate",
  "key": {"mode": "managed", "phase": "intent", "public_fingerprint": null},
  "nodes": {"0": {"state_key": "example/compute/nodes/0.tfstate", "role": null, "index": 0, "phase": "declared", "operation_id": "unique-attempt-uuid"}}
}
```

No passwords, tokens, private keys, complete desired-state secrets or raw tofu
state enter this object. Provider resource IDs, public key fingerprints and
non-secret state lineage/serial observations may be added for reconciliation.
Do not persist arbitrary collected metadata without a reviewed allowlist.
ETags are opaque transport validators; do not recompute them from JSON or
interpret them as hashes. The revision is for diagnostics, not a substitute
for the ETag condition. A unique write_id prevents identical-body ABA changes.

Backend identity is independently checked against the deployment's reviewed
configuration. Finding a missing object at a different bucket/endpoint cannot
prove a fresh deployment; backend changes require the existing migration
contract. Cross-backend local SSH key/profile collisions remain separate checks.

## Acquire, update and release protocol

1. Read the object using the authenticated direct S3 endpoint, bypassing public
   caching. Classify confirmed NoSuchKey separately from denied access, missing
   bucket, transport errors, parse errors and unsupported schema. No failure
   other than established object absence permits first-create behavior.
2. On absence, conditionally create the initial journal with held lock using
   `If-None-Match: *`. Do this before SSH generation or provider changes. On a
   readable idle object, validate identity and acquire by conditional replacement
   with `If-Match` against the observed ETag and a fresh run_id/write_id.
3. A held lock belongs to its recorded run. Competing invocations fail with
   non-secret owner/run diagnostics. A recent heartbeat is diagnostic only;
   an old timestamp never grants takeover. Matching run_id is not enough for
   a second process to attach: only the still-running coordinator owns it.
4. Every journal transition checks held run ownership, increments revision,
   supplies a fresh write_id, and uses `If-Match`. The coordinator serializes
   its writers; branch workers never independently overwrite the journal.
5. After a write timeout, GET and compare write_id/run_id/revision to determine
   whether that exact write committed. If the result cannot be established,
   stop dispatch and preserve the lock. Never blindly replay a provider action
   merely because an object-store response was lost.
6. After all child operations have terminated and outcomes are durably recorded,
   release by CAS to `lock.state = idle`. Do not delete the lock object. On a
   caught failure, release only after proving no child remains active; if that
   cannot be established, retain held state for recovery. Process exit by
   itself is not evidence that child tofu processes have stopped.

There is no multi-object transaction in this design. Keeping ownership and
journal together makes acquisition and declaration of attempted nodes atomic
at the object level. Native state locks continue protecting each state write.
The protocol assumes cooperative library callers and protected credentials;
it cannot prevent an operator using direct provider APIs or an unconditional
S3 overwrite. IAM restrictions and deployment policy must reflect that boundary.

## Crash-safe lifecycle transitions

Before dispatch, CAS-record the complete intended node set and every state key.
Mark nodes `create-intent` before spawning provider operations. Record provider
results only after state/output validation, then mark `ready`. A coordinator
must persist completion even if a sibling failed. A failed join prevents app
convergence but must never erase successful/attempted nodes.

Shared key preparation records intent before filesystem mutation. Generate
once, verify the complete pair and fingerprint, then record `prepared`. If a
crash leaves a local key without confirmed matching prepared ownership, fail
closed and require explicit recovery; the intent alone does not authorize
adoption. This preserves the SSH standard's ambiguous-interruption refusal.

A node absent from current topology becomes `retire-intent`, never disappears
from the journal. Application quorum/teardown checks authorize destruction;
write `destroy-intent` before it begins. On verified destruction mark it
`destroyed`, retaining history until deployment retirement. For delete, enumerate
recorded attempted/retired nodes, not just current desired nodes; no complete
create join is required to clean up partial creation.

A failed provider apply may have created a resource without saving complete
state. Therefore a missing node state after a recorded create intent is not
proof of no cloud resource. Reconcile provider identities/tags or report
uncertainty; do not blindly provision again or declare cleanup complete.
Automatic import is outside this initial protocol and requires ownership proof.

After all node destruction is verified, destroy shared resources. Keep the
coordination record. Then remove owned local key files and confirm deletion.
Only after that succeeds, CAS status to `retired` and release. Retain this
non-secret tombstone; no automatic garbage collection is proposed. A later
recreation uses an explicit new generation while preserving prior history.

## Stale-lock recovery

Provide a separate recovery command that inspects ownership and recorded
operations, never an automatic lease sweeper. Recovery requires evidence that
all old launcher/tofu processes are terminated and outstanding cloud operations
are reconciled. Another machine's process absence cannot be inferred locally.
If termination cannot be established, do not unlock.

Once quiescence is established, recovery CAS-transfers the held journal to a
new recovery run_id against its current ETag and retains recovery provenance.
Use per-state lock inspection separately; do not call force-unlock for all
states automatically. Killing a process does not cancel an already accepted
cloud API operation, so resource reconciliation remains required before retry.
An epoch value can diagnose stale writers, but cannot fence Azure/AWS/etc.
mutations without provider enforcement; do not advertise it as such.

## Concrete next implementation

1. Add a pure `coordination` reducer in all three colors with strict schema,
   identity checks and legal acquire/declare/start/complete/retire/release
   transitions. Inputs include observed object+ETag and requested event; outputs
   are CAS intentions and allowed actions. No filesystem/network operations.
2. Add shared JSON parity fixtures for two competing runs, stale ETag, ABA body
   repetition, lost-response readback, invalid schema, partial node creation,
   scale-down, interrupted key preparation and cleanup, and retired tombstones.
3. Implement an injected transport interface: `get`, `put_if_absent`,
   `put_if_match`, returning tagged present/absent/precondition/error outcomes.
   Use a maintained S3 SDK/CLI transport with explicit R2 credentials or the
   ambient AWS chain. Never reuse R2 access keys as global AWS variables for
   compute commands. Do not build a bespoke SigV4 signer without need.
4. Build a single coordinator wrapper around the Colors workflow. Persist
   intents before dispatch, serialize completion updates, track every child,
   stop new work on ownership uncertainty, and implement reliable wait/kill
   behavior before release. Integrate protected backend initialization and
   state read classification rather than only wrapping the current tofu step.
5. Test with a deterministic fault-injecting fake object store and fake tofu
   processes first. Then run separately authorized disposable S3/R2 object
   probes: competing conditional creation, conditional update with stale ETag,
   ambiguous response recovery, and actual OpenTofu native-lock contention.
   These probes mutate isolated test objects and are not part of this assessment.
6. Only after those checks, implement a fresh disposable cluster lifecycle and
   prove partial failure, retry, delete and final-key cleanup. Existing live
   deployment migration remains separately gated by ownership/state mapping.

Handoff: the recommended next coding slice is the pure reducer and parity
fixtures (steps 1–2), independently reviewable before transport and cloud
execution. The current assets and planning functions do not satisfy deployment
coordination until the runtime and endpoint evidence above exist.
