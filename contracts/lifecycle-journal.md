# Lifecycle journal, schema 2

Schema 1 remains readable by its original reducer. Production orchestration
uses schema 2 and event names prefixed `lifecycle/`. Existing schema-1 journals
require explicit migration; they are never silently upgraded. The same journal
transport accepts either strictly validated schema. The coordinator chooses
schema 2 through its reducer/event-prefix option and otherwise retains its
serialization and ambiguity rules.

Schema 2 has exactly schema_version, identity, revision, write_id, lock,
generation, status, topology_declared, key, shared, nodes. Identity, lock, safe
IDs, revision and conditional-write rules match schema 1. generation is a safe
positive integer. status is active, deleting or retired. Key has exactly mode,
phase, fingerprint. Mode is null, managed or external. Phase is absent, intent,
prepared, cleanup or removed. Absent requires null mode/fingerprint; managed
prepared/cleanup/removed requires a nonblank SHA256 fingerprint; external uses
null fingerprint. Shared has phase, operation, operation_id. Nodes have the
same three operation fields plus state_key, role, index and desired. Phases are
declared, running, ready, failed, destroying, destroyed. Operation is null for
declared, otherwise create or destroy. Running requires create; destroying and
destroyed require destroy. All other phases retain the attempt operation.
Operation IDs are safe and unique across shared and all nodes. Initial declared
records have null operation/ID. Node identities and state keys are derived as
in schema 1; index is 0..999. desired is boolean. Historical nodes remain in the
map through scale-down and deletion. There are at most 1000 total records.
All schemas reject unknown fields and non-integral/boolean numbers.

Every event has type, run_id, write_id, target_etag. The suffix determines these
additional fields:

- acquire, release, begin-delete, retire, recreate: none.
- declare: topology, using the existing topology expansion and 1000-node bound.
- key-intent: mode, managed or external.
- key-prepared: fingerprint, SHA256 string for managed or null for external.
- key-cleanup, key-removed: none.
- shared-start, shared-destroy: operation_id.
- shared-retry: evidence, exactly readable-state.
- shared-complete, shared-fail: operation_id.
- start, destroy: node_id, operation_id.
- complete, fail: node_id, operation_id.
- retry: node_id, evidence, exactly readable-state. This event only changes a
  failed create attempt to declared after runtime confirms readable owned state.
  Missing state after failed create never authorizes a blind retry. Shared retry
uses the same rule for shared state.

Acquire creates a fresh active journal with generation1, absent key, declared
shared, and no topology/nodes, or acquires an idle schema-2 journal. Held locks
always refuse. Other events require held ownership, correct ETag and fresh ID.
Write conflicts/ambiguity follow the coordinator contract; no phase grants
permission until its CAS commits. Validation errors use fixed messages without
input values. Prefix errors with `invalid lifecycle identity/event/document`,
`lifecycle read failed`, `lifecycle identity mismatch`, `lifecycle stale observation`,
`lifecycle write_id reused`, `lifecycle revision exhausted`; transition refusals
use `lifecycle transition refused` except held/owner errors which use
`lifecycle lock held`, `lifecycle lock not held`, `lifecycle owner mismatch`.

Declare is allowed only while active and no shared/node operation is running.
It records the entire intended topology before dispatch, preserves old outcomes,
marks removed nodes desired=false and retains them for destruction. Requested
previously destroyed nodes become declared. Unknown nodes are added declared.
It does not reset failed attempts. Set topology_declared=true.

Key-intent requires active status and absent key. Key-prepared requires intent.
Shared-start requires active status and prepared key, and shared declared or
ready. Node start requires active status, desired=true, prepared key, shared
ready, and node declared or ready. These start events record running/create
and fresh operation_id. Shared/node completion requires matching active attempt;
create becomes ready, destroy becomes destroyed. Failure becomes failed and
retains operation and ID. Retry is allowed only on failed/create with runtime
readable-state evidence, and resets to declared without dispatching anything.

Begin-delete requires no running work; it sets status deleting and every node
desired=false. Node destroy requires desired=false, phase not running/destroying/
destroyed, and records destroying/destroy with a fresh operation ID. Shared
destroy requires every node destroyed and shared not running/destroying/destroyed.
Key-cleanup requires deleting status, all nodes/shared destroyed, and key prepared. Key-removed requires cleanup. Retire requires deleting, key absent or removed,
and all resources destroyed; set retired. Release refuses any running/destroying
work or key intent/cleanup; it keeps all history and sets idle/null. Failed
resources may be released only after runtime has established process quiescence.

Recreate requires retired status and no running work. Increment generation,
set active, reset key to absent and shared to declared, retain destroyed node
history, and clear topology_declared until the next declaration. File collision
checks still apply. Repeated delete of retired state is a no-op at orchestration,
not permission to remove unrelated files.

This journal records orchestration evidence, not resource IDs or secret output
maps. Runtime validates owned remote state before retry/delete and retains the
lock on uncertain child termination. Provider outputs flow in memory to the
Colors join and Ansible. Direct API actors outside the coordinator remain outside
its exclusion guarantee.

Strict document invariants: deleting requires every node desired=false. Retired
requires key absent or removed, shared and all nodes destroyed, and every node undesired.
Key cleanup/removed requires deleting or retired status and all shared/node
resources destroyed. These invariants prevent forged tombstones from authorizing
recreation. Desired node indices are contiguous within each current role;
historical undesired nodes may use different roles, including mixed old null
and named roles. SHA256 fingerprints match `SHA256:[A-Za-z0-9+/]{43}` exactly.
Shared-start also requires topology_declared=true and no running/destroying
shared or node attempt. Shared-retry has only evidence=readable-state and resets
a failed/create shared attempt to declared, without dispatch. Key-cleanup is
permitted only from prepared, never from uncertain intent.


An absent-key retirement preserves mode=null and fingerprint=null. Runtime must
first verify that every shared/node record is declared or destroyed and that
its state is absent or a verified empty object. It then records destroy and
completion for each declared node, followed by shared, before retire. It never
runs OpenTofu or touches SSH files. Any ready, failed, running or destroying
record prevents this shortcut. Removed-key deletion can finish retire after the
same state checks without repeating cleanup.

Interrupted key intent and cleanup retain their locks. Follow
[manual key-phase recovery](key-phase-recovery.md) after proving the original
process and its children have stopped. A held lock never expires.
