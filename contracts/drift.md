# Serialized compute drift checks

`check_deployment_drift(opts, topology, requirements, environment?, dependencies?)`
(Blue/Red) and `check-deployment-drift` (Green) return `{status:"clean"}` only
when every owned shared/node state produces an OpenTofu zero-change plan. Any
failure returns `{status:"error"}`. Missing compute credentials may include the
existing safe `errors` list. Cancellation propagates after lease release is
attempted; an uncertain journal release never reports clean.

This operation requires an existing, valid schema-2 journal, active deployment,
idle lock, declared topology, prepared key, ready shared state, and exactly the
requested ready/desired nodes. Historical destroyed nodes may remain recorded;
failed, pending, extra surviving or missing nodes refuse. The existing journal
is acquired through conditional writes before state reads and retained for all
plans. A missing journal is never created by this operation. Acquisition and
release are the only journal changes; no node/shared/key transitions occur.
There is no timed takeover. The lease coordinates library clients, not external
actors that mutate resources or backend objects directly.

All state reads use the protected backend reader and normal node collection.
Role requests use the observed joined private addresses for their final shared
peer rules. Managed keys read only the existing regular `.ssh/<profile>.pub`,
with a 64 KiB limit, no final symlink following, strict UTF-8 and fingerprint
matching against the journal. No private key is read, chmod performed, keypair
created, or SSH ownership changed. External references use the existing public
key resolver. Missing local public files fail closed.

The internal `check_state` / `checkState` / `check-state` primitive reuses the
private OpenTofu directory, reviewed document validation, ambient compute auth,
private R2 backend credential file, sanitized environment, timeout and cleanup
from the guarded executor. It requires a present, nonempty v4 state owned by the
selected provider and runs:

```
tofu init -input=false -no-color -reconfigure -backend-config=<private-file>
tofu state pull
tofu plan -input=false -no-color -detailed-exitcode
```

Only exact exit zero passes. Exit 2 (drift), any failure or signal refuses; no
plan is applied, no retry occurs, and no provider diagnostic or credential is
returned. Shared state and each node state are checked sequentially while the
same deployment lease remains held. An application may retain its separate DNS
or other non-compute drift gates.

Dependency hooks use each language's existing conventions: journal_get/put,
read_state, public_key, key_request, compute_credential_errors and check_state
(or their hyphenated Green names). These are trusted test/integration hooks,
not user input. The native public API does not accept pre-approved plans or
caller-selected arbitrary state keys.
