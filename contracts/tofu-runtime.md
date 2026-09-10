# Guarded single-state OpenTofu execution

`state_presence(opts, state_key, environment, runner)` directly reads the selected
backend object with AWS CLI GetObject in a private session, discarding the body.
It returns absent only for exact NoSuchKey, present only on successful GetObject,
and error otherwise. Reuse journal authentication/error protections. Accept only
`<profile>/compute/shared.tfstate` or `<profile>/compute/nodes/<safe-id>.tfstate`.
No state write is exposed by this operation. Do not infer absence from tofu's
empty stdout. State presence alone does not establish ownership of orphaned
provider resources after a failed apply.

An optional legacy=true argument also permits `<profile>/<safe-id>.tfstate`.
The orchestrator uses this read-only check to refuse old monolithic state before
creating a new layout. It grants no write or adoption permission.

`converge_state(opts, state_key, documents, operation, presence, environment,
runner)` handles one shared or node state. operation is create or delete;
presence comes from the confirmed object observation above. Caller MUST hold a
schema-2 coordinator lock and have committed the matching attempt intent before
invoking it. No public package calls it outside lifecycle orchestration. Delete
requires compute-prevent-destroy false. Error/unknown presence refuses before
any command. Present state must retain the selected provider or be verifiably
empty after a previous destroy. Legacy outputs lacking provider and mismatched
providers refuse mutation. Retry after failed creation needs separately confirmed
readable owned state, not merely absent state.

Use a new 0700 directory and 0600 generated JSON files/private backend config.
Documents are a map of safe `*.tf.json` basenames to rendered JSON objects.
No arbitrary filenames, symlinks, provider overrides from callers, or private key
contents. Bind R2 keys only to private backend configuration. Bind selected
compute provider tokens to their registry tofu-env names from COLORS_PAR_ env.
Remove inherited TF_*, TOFU_*, COLORS_PAR_* controls and set automation, input,
default workspace and private TF_DATA_DIR. Preserve ambient AWS/Azure/Google/OCI
authentication. Unlike the separate read-only R2 reader, a combined compute
session MUST retain AWS_PROFILE because AWS compute may need it. Do not map R2
keys to AWS environment variables. All diagnostics remain private/generic.

Fixed command sequence using native exact-environment runner:

1. tofu init -input=false -no-color -reconfigure -backend-config=<private file>
2. tofu state pull. Parse any nonempty result as strict v4 state. Empty is
   acceptable only with confirmed absent presence. An already empty v4 state has
   no resources and no outputs. For delete it is an idempotent destroyed result.
3. tofu plan -input=false -no-color -out=<private plan>, adding -destroy for delete.
4. tofu show -json <private plan>. Require an object with format_version and
   planned_values object; inspect all resource_changes change.actions. Create
   permits only no-op/create/update/read, refusing every delete/replacement.
   Delete permits only no-op/delete/read. Unknown actions or malformed plan
   refuse. No automatic replacement or state migration.
5. tofu apply -input=false -no-color <private plan>. Exit must be exactly zero.
6. tofu state pull. Create requires valid v4 state and params.provider matching
   selection; return `{status:ready,params:...,outputs:...}`. Outputs are flattened
   non-sensitive values needed to pass shared network and key references to
   nodes. Delete requires no remaining
   resources and outputs, or empty stdout after the successful destroy; return
   `{status:destroyed}`. Any failure returns only `{status:error}`.

On confirmed absent presence, delete is already destroyed and runs no commands,
provided the caller's journal/ownership checks have ruled out orphaned attempts.
This is a state execution result, not proof that unknown external resources do
not exist. Timeouts: init/state/show 120seconds, plan/apply 30minutes. Cancellation
must terminate and await owned subprocess work, clean temporary files, propagate,
and leave coordinator ownership uncertain. Never log raw plans/state or known
credentials; reject known credential echoes in returned params. Cleanup failures
are errors. The private plan/cache can contain credentials and must be removed.

Tests use injected runners to prove ordering, credential separation, absent/error
refusal, mismatch/legacy refusal, replacement refusal, failed apply, post-apply
validation, delete guards, idempotent cleanup, permissions and cancellation.
Provider schema checks and loopback backend probes remain separate evidence.

The state reader's optional include_outputs flag returns flattened outputs and
state_empty. The latter is true only when the validated v4 envelope has no
resources and no outputs. A retained empty state object is valid after destroy;
its presence alone must not block recreation. Failed attempts still require
readable provider ownership and never treat an empty object as recovery evidence.

## Selective VPC drain retry

Reviewed `contracts/execution-policy.json`, packaged with each library, currently
permits one retry class: DigitalOcean shared documents owning a
`digitalocean_vpc`, during delete only, when the apply error contains the exact
provider message `Can not delete VPC with members`. Error classification is
bounded to 1 MiB and refuses known credential values. Other errors, providers,
node documents, creates and drift checks never retry.

At most four apply attempts run, with 30 seconds between attempts. Before each
retry the executor pulls and validates the partial state, requires the selected
provider, and creates and validates a fresh destroy plan. Empty valid state
confirms completion. An unreadable or malformed partial state refuses. The same
journal shared attempt remains active throughout; there is no second node
operation or blind replay of an old saved plan. The existing timeout applies to
each subprocess. Tests inject a sleep function to avoid real waiting (seconds
in Python, milliseconds in Red/Green); native callers use the fixed delay.

On a freshly initialized remote backend, OpenTofu 1.12.5 can emit an empty
version-4 state with serial zero and an empty lineage. Execution recognizes
that exact initial envelope only when an independent state-presence read has
already confirmed the object absent. The same envelope from a present state,
nonzero serial, resources, outputs, or unexpected fields remains a refusal.
Existing state decoders continue to require a nonempty lineage.
