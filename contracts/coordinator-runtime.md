# Runtime journal coordinator

The coordinator owns one invocation and serializes all conditional journal
transitions. It uses the existing reducer and journal transport. It never treats
a planned write as committed, retries a provider operation, or takes over a held
lock. Native APIs expose a Coordinator with acquire, declare, start, complete,
fail, release and snapshot methods, following language naming conventions.

Constructor receives opts, optional environment, optional read/write callbacks,
and optional ID factory for tests. It snapshots opts/environment. Defaults use
journal_get/journal_put and cryptographically random UUIDs. Read takes no
arguments; write receives one intention. Every generated ID must satisfy the
reducer grammar and must not repeat within the coordinator. Construction must
not read or write remote state. Normalize expected identity from opts using the
same profile/provider/backend fields as journal_put.

All methods serialize through one per-instance mutex/queue. acquire reads the
journal once, then plans acquire with its observed ETag and a fresh run/write ID.
It is allowed only once for the instance. Methods before successful acquire or
after release refuse. A held journal always refuses even if a supplied test ID
matches. snapshot returns a detached copy of confirmed observation only.

To commit any transition:

1. Compute the intention through coordination with the confirmed observation.
2. Submit the intention once. A valid written response with a nonblank ETag
   confirms the exact document sent. Keep a detached observation of that document.
3. On an error result or ordinary write exception, read back once. Accept only a
   present observation with a nonblank ETag whose complete document structurally
   equals the intended document, including write_id. NoSuchKey, malformed
   response, an older document, or a different writer does not confirm it.
4. Conflict immediately invalidates ownership. Unknown responses also invalidate
   it. Do not retry the write. Mark the coordinator poisoned, refuse further
   transitions, and keep any existing remote lock for recovery. An unconfirmed
   ambiguous write does the same. Cancellation propagates and poisons the instance.

Reducer validation errors before a write do not poison confirmed ownership.
Transport/ownership failures expose only `coordination ownership uncertain`.
Do not propagate callback exception text or raw remote documents in errors.

`declare(topology)` records the complete topology. `start(node_id)` allocates a
fresh operation ID and commits start before returning it. Only then may the
caller dispatch that attempt. Keep an in-memory active map of node->operation.
`complete(node_id, operation_id)` and `fail(node_id, operation_id)` require the
matching local active attempt and assert that its child work has terminated.
Remove the local attempt when these methods run, even if committing the outcome
fails. They do not cancel or join arbitrary user processes. Caller orchestration
must supply that evidence. Wrong IDs refuse with `coordination local attempt
mismatch`. `release()` refuses nonempty active attempts, a poisoned coordinator,
or reducer-recorded running nodes. It becomes terminal after confirmed release.
Never auto-release in a destructor or catch-all finally block.

Tests must use a fault-injecting conditional object store and cover one winner
among contenders, serialized parallel starts/completions, successful readback of
a committed response lost in transit, unmatched readback poisoning, conflicts,
ordinary exceptions, cancellation, no repeated writes, failed siblings retained,
release while active refused, immutable snapshots and lifecycle misuse.
This class supplies transport coordination. Shared-resource phases, process
supervision and provider work still belong to the lifecycle orchestration.
