# Single compute unit contract

This contract replaces deployment coordination, lifecycle journals, cluster
orchestration, managed-backend lifecycle, and workstation-owned SSH keys.
All three colors must implement the same pure plan and observable lifecycle.

## Boundary and identity

One invocation addresses one persistent compute unit. Multiple invocations of
that unit reuse its identity, state, and working directory. The caller supplies
unique identifiers for different units and uses Colors SDK for fan-out, joins,
ordering, scaling, and migration. No topology, role expansion, cluster inventory,
or cross-node cleanup is part of this API.

`node_plan(opts, request)` is pure. `build_node(opts, request)` writes its templates
without cloud calls. `compute_node(opts, request, operation, environment,
dependencies)` executes one unit. Green uses kebab-case native names with `!`
on build and execution. Operations are `build`, `create`, `inspect`,
`prepare-access`, and `delete` (create is the default). Build returns the plan
with status `built`; the pure plan has status `planned`. Successful runtime
results have `status: ready`, `directory`, and `params`; destruction returns
`status: destroyed` and `directory`. Create and prepare-access add
`params.ssh_identity_file` only after authoritative-key verification. Inspect performs
no key download. Runtime failures return `status: error`; cancellation propagates.
Pure validation/build errors contain no credentials or raw provider data.

Request fields are `node_id`, `state_filename`, `workdir`, `security`, and optional
`network`. Node and profile identifiers are safe single path components; filename
is a single `.tfstate` filename, never an arbitrary path. Workdir is an absolute
SDK directory. Path traversal, symlink substitution, unknown request fields, and
Terraform expression injection in caller values must be refused.

The working directory is `<workdir>/<profile>/<node_id>`. State key is
`<s3-prefix>/<profile>/<state_filename>`; an empty prefix omits its separator.
Prefix components must be validated; the library never invents a node filename.
S3/R2/OCI use the state bucket for key objects. Local state owns its SSH keys
locally, requires no S3 settings, and returns an empty `key_objects` map.
GCS state requires
`ssh-s3-bucket` and `ssh-s3-region`, with optional HTTPS `ssh-s3-endpoint`.
Those explicit stores use ambient AWS authentication unless both
`COLORS_PAR_SSH_S3_ACCESS_KEY_ID` and `COLORS_PAR_SSH_S3_SECRET_ACCESS_KEY`
are supplied; a partial pair is refused. R2/OCI use their corresponding
`COLORS_PAR_<BACKEND>_*` credentials. An explicit key-store override on S3/R2/OCI
must not silently redirect authoritative key storage. Local backends reject
`ssh-s3-*` options.

Remote key objects use `<s3-prefix>/<profile>/<node_id>/ssh-key` and `ssh-key.pub`.
Distinct callers must not reuse identifiers or state filenames for different
units. Renaming a unit or changing a state key is migration, not an update.

## Persistent OpenTofu root

Build renders one root configuration containing the machine, exclusively owned
supporting resources, TLS key generation, backend config, and (for remote
backends) S3 SSH key objects.
Shared/node template fragments may be reused internally, but there is one state
and one apply, with no intermediate shared state or externally visible stages.
Provider expressions wire dependencies inside this unit only.

OpenTofu runs in the unit directory. Do not set `TF_DATA_DIR`, remove `.terraform`,
or automatically delete templates, lock files, or the workdir. Build can update
its owned templates but cannot discard files needed to destroy existing state.
Persistent directories/files are private. Runtime credentials must not be
embedded in rendered templates; cached backend settings, plans, and state can
still contain secrets and require restrictive permissions.

Native backend locks protect state mutations. There is no journal, custom lease,
conditional ownership object, or cluster-wide lock. The caller must serialize
operations that share a local unit directory, including build and initialization;
the native state lock does not serialize arbitrary filesystem writes or Ansible.

## SSH authority

`tls_private_key.machine` generates an ED25519 keypair. For remote backends, the two S3 object
resources and any compute-provider key registration are in the same state as
the machine. The machine depends on the remote key objects so destruction tears
down the machine first. The object storage provider is separately aliased from
an AWS compute provider; R2/OCI credentials must not replace AWS compute auth.

Remote objects are the only source for local Ansible key material. Download both,
validate that the private/public pair and recorded public fingerprint agree,
then atomically replace private local files. A local key is never input to build,
apply, upload, ownership adoption, or recovery. Retrieval failure fails the step
and must not authorize access with a stale copy. No private key appears in API
outputs, diagnostic messages, command arguments, or rendered templates.

For the local backend, the local OpenTofu state is authoritative. The
`ssh_private_key` (sensitive) and `ssh_public_key` outputs supply the access pair, which is
verified and refreshed with the same checks as remote downloads. These outputs
are internal to the runtime, never exposed in public API results. Local key
copies without state refuse creation rather than being overwritten with a new
identity. See [local backend](local-backend.md).

Terraform state and saved plans necessarily contain generated key material.
Read access to them is secret access even though the local copy is disposable.
Use encrypted private storage and least-privilege object access. S3 object
resource deletion must cover its versions according to the pinned provider;
retained state versions/backups can retain historical key material. Legal holds
must not be bypassed automatically. Key deletion occurs only after node destroy.

## Execution and recovery

Create initializes the persistent root, observes and validates existing state,
refuses a mismatched provider or unit identity, creates a saved plan, validates
its actions, applies, validates resulting state, and refreshes local SSH copies.
Do not use `-lock=false`. Create refuses delete/replacement actions; delete
requires explicit `compute-prevent-destroy: false` and refuses create/update
infrastructure actions. Read errors never mean absence. Required-existing-state
refuses absent state. No automatic provider migration, import, force-unlock,
resource adoption, or retry of uncertain mutation is performed.

Inspect validates state and returns normalized node outputs. A readable state
with both empty resources and empty outputs returns `status: destroyed` from
inspect without mutations, allowing the caller to resume local cleanup.
Missing state remains an error. Access preparation
retrieves current authoritative keys; it is not satisfied by existing local files.
Delete refuses independently absent state: it cannot prove that lost state has
no surviving cloud resources. A valid, strictly empty state allows idempotent
local cleanup. Delete destroys the unit through its original configuration and backend, then
removes disposable local keys. It retains templates and initialization files.
Exceptions and cancellation stop dependent work, terminate owned local
subprocesses, and preserve recovery files. An interrupted cloud operation can
outlive its local process; the caller/operator must reconcile before retry.

Remote key objects found without corresponding owned state must not be
overwritten. Missing state cannot prove resource absence after a previous failed
apply. Recover state or inspect/import resources explicitly before another create.
No substitute ownership journal is introduced to reconstruct that history.

## Compatibility and verification

Provider migration uses distinct source/destination units. Both directories,
keys, and states remain available until the caller completes cutover and
explicitly destroys the source. Existing shared/node states and journals require
reviewed state transfers before adopting this API; never delete old ownership
records as a shortcut.

Tests must exercise persistent workdir behavior, profile/node/key isolation,
all-provider rendering, provider-mismatch refusal, replacement/destruction
guards, secret separation, authoritative remote key overwrite, remote read
failure, partial key copies, and deletion ordering. Three-color parity compares
complete plans, not only state path strings. Schema validation verifies the
combined root and aliased object provider without live infrastructure calls.
