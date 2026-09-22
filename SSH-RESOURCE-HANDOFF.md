# Handoff: separate SSH resources, compute resources, and agent sessions

Historical design record. The implemented API is defined in
[contracts/ssh-resource.md](contracts/ssh-resource.md) and
[contracts/node.md](contracts/node.md); the status below describes the handoff
before implementation.

Status: design handoff only. No implementation, key generation, state migration,
consumer pin updates, or live infrastructure changes have been performed.

## Agreed design decisions

These decisions were confirmed with the user. They define the intended new API;
implementation details listed below remain open.

- Support both local and remote backends, following the Terraform backend model.
  Local SSH resources live inside the Colors SDK workdir under a profile
  subfolder. Remote resources use the corresponding workdir/profile namespace.
  Exact filesystem and object-key layouts remain to be specified.
- Inherit the workflow backend configuration by default, with an explicit
  per-resource override for a different backend or bucket.
- Give every SSH resource an explicit, stable name within its profile, for
  example `app-access` or `database-access`. Renaming requires an explicit
  migration to preserve identity.
- Each SSH resource has its own passphrase binding. Users may explicitly supply
  the same secret to multiple resources; there is no mandatory shared passphrase.
- Each compute node explicitly references its SSH resource in the SDK graph;
  there is no implicit default key. Two SSH resources can feed two separate
  fan-outs of colors-compute nodes.
- Missing encrypted-key authority for an existing resource fails closed and
  requires explicit recovery. Never silently generate a replacement key.
- Serialize creation, rotation, and deletion with a lock scoped to backend,
  profile, and SSH resource name. Independent SSH resources may proceed in
  parallel. The lock mechanism and crash-recovery protocol remain open.
- Use one dedicated agent session per workflow scope, loading the identities
  that scope needs, including multiple independently managed SSH resources.
- Model provider public-key registrations as explicit shared resources, each
  owned once and referenced by consuming compute nodes.
- Keep SSH resource deletion separate from compute deletion. Deleting a compute
  fan-out must not delete its SSH resource.
- Rotate passphrases only through an explicit operation requiring old and new
  passphrases and preserving the public key. Changing a configured secret alone
  triggers neither rotation nor regeneration.
- Implement and validate the new API alongside the existing API, then migrate
  deployments individually. Do not silently reinterpret existing state.

## Intended direction

Replace `tls_private_key.machine` with encrypted OpenSSH key generation using
`ssh-keygen`. Expose three independently composable capabilities from
`colors-compute` so the Colors SDK owns their DAG:

1. Create or retrieve a durable SSH resource.
2. Create compute resources that consume its public identity.
3. Start a temporary SSH agent that unlocks the key for Ansible and SSH checks.

The user's proposed ordering is one SSH resource followed by N compute resources
and one agent session in parallel. Application convergence waits for the compute
results and the agent. Support this composition without moving topology or DAG
coordination back into `colors-compute`.

```text
                    +--> compute 1 --+
SSH resource ready -+--> compute N --+--> join --> Ansible / SSH checks
                    +--> agent ------+

SDK scope cleanup: stop owned child processes, then terminate owned agent.
```

The cleanup above is guaranteed scope cleanup, not an ordinary success-path DAG
node. It must run when a compute branch, agent setup, or application step fails
or is cancelled. Agent startup could alternatively be deferred until access is
needed; parallel startup is the requested composition.

## Current implementation and why this is a contract change

Read `README.md`, `contracts/node.md`, `contracts/local-backend.md`, and
`migration/README.md` before implementation. There was no root `CLAUDE.md` or
`AGENTS.md` in this checkout when this handoff was written; workspace
instructions still apply.

The current single-unit API makes each compute unit own its SSH identity:

- `tls_private_key.machine` generates ED25519 material during OpenTofu apply.
- Remote backends persist the private/public pair in S3-compatible objects
  managed in the same state as the machine. Local backends use state outputs.
- `create` and `prepare-access` retrieve the authoritative pair, verify it
  against the recorded fingerprint, and return `params.ssh_identity_file`.
- Local copies are disposable; missing or unreadable authority refuses access.
- State and saved plans contain plaintext private-key material.

Implementation entry points:

- Green: `green/src/clj/io/github/getcolors/compute_node.clj`
- Red: `red/src/node.ts`
- Blue: `blue/src/colors_compute/node.py`

Separating SSH ownership from compute explicitly supersedes the current
per-unit key ownership contract. Version the API and document migration; do not
silently reinterpret existing states or update consumer pins.

## Proposed interfaces and ownership

Names and exact result schemas remain to be finalized; these are responsibilities,
not already implemented functions.

| Capability | Owns | Returns |
| --- | --- | --- |
| SSH resource | Stable identity, encrypted key generation/storage, recovery, fingerprint, eventual deletion | Resource reference, public key, fingerprint |
| Compute resource | Machine and exclusively owned provider supporting resources | Compute identity, address, user, other normalized outputs |
| Agent session | Dedicated local agent process/socket, key loading, verification, cleanup | Scoped socket path and owned cleanup handle |

Compute explicitly references its SSH resource and consumes only its public
identity. It must not need the private key or passphrase. The agent resolves the encrypted key
reference and verifies that the loaded identity matches its recorded fingerprint.
API results must never contain passphrases or decrypted private keys.

One key feeding N machines deliberately shares an access identity. Keep sharing
explicit in the SDK graph. Also support N independent SSH resources loaded into
one agent, without implicitly collapsing their identities.

Some providers require an uploaded/registered public-key resource. Model each
registration as an explicit shared resource with one owner, referenced by compute
nodes rather than independently declared by all N nodes. Provider/account/
region scoping may require multiple registrations for one public key. Preserve
exclusive ownership of all other supporting compute resources.

## Passphrase and encrypted storage

Each SSH resource has its own runtime passphrase binding, with explicit secret
reuse allowed. The environment-variable naming and binding schema remain to be
specified; the earlier proposal of one `COLORS_PAR_SSH_KEY_PASSPHRASE` variable
is insufficient by itself for independently configured resource passphrases.

- Read it only at runtime. Reject missing or empty values for generation and
  unlocking before dependent mutations. Build remains credential-free.
- Generate ED25519 directly into encrypted OpenSSH format. Do not generate a
  plaintext file and encrypt it afterward for new resources.
- Do not pass the secret with `ssh-keygen -N` or `-P` arguments. Implement and
  test a controlled prompt/askpass mechanism on supported macOS and Linux
  OpenSSH versions. Never embed the secret in a helper script.
- Do not persist the passphrase in YAML, templates, state, plans, logs, or API
  results. Narrow its child-process environment to the unlocking mechanism;
  Ansible, providers, and unrelated processes do not need it.
- Keep directories private and files mode 0600, with existing symlink and
  unsafe-file protections. Never write a decrypted access copy.
- Choose and document an explicit KDF policy; benchmark its effect when loading
  multiple keys instead of depending accidentally on host OpenSSH defaults.

The encrypted key must be durable before any machine is created. OpenTofu may
continue managing remote objects containing ciphertext: ciphertext in state is
acceptable, but plaintext private material and the passphrase are not. This is
a candidate design, not a settled storage implementation.

Define local-backend authority explicitly; simply deleting the TLS resource
removes today's authoritative source. A durable encrypted local resource cannot
be treated like the current disposable access copy.

Missing authority for an existing identity must fail closed. A retry must reuse
the same key, never silently generate a new one. Passphrase rotation must preserve
the public key and use an explicit old/new-secret protocol; a changed environment
value alone must not trigger rotation or regeneration.

## Coordination and recovery decisions to resolve first

Moving generation outside OpenTofu means its native state lock does not protect
the generation/upload sequence. Specify these cases before coding:

1. Two concurrent first creates for the same SSH resource.
2. Interruption after generation, after either object upload, or before state
   records ownership.
3. Existing remote objects with missing state, or state with missing objects.
4. Recovery from a different workstation without adopting an arbitrary local key.
5. Concurrent passphrase rotation and access preparation.

The durable identity uses the backend, profile, and stable SSH resource name.
Creation, rotation, and deletion must share a lock at that scope. Specify its
implementation, authority records, and atomic creation/recovery protocol, including
how access preparation coordinates with rotation.
Do not casually introduce a new journal or custom lease: the current contract
explicitly excludes them, so any such mechanism requires a documented contract
decision. Preserve the rule that failed reads do not establish absence.

## Agent lifetime and SDK integration

The SDK/caller owns the execution scope because compute returns before Ansible
runs. Starting and stopping an agent entirely inside a compute function would
end the session too early.

- Start one dedicated agent per workflow scope with a private socket; do not mutate the operator's
  existing agent or global process environment.
- Load only the identities needed by that scope and verify their fingerprints.
- Pass `SSH_AUTH_SOCK` and explicit identity selection to dependent Ansible/SSH
  processes. Test existing `IdentityFile`/`IdentitiesOnly` behavior and multi-key
  sessions rather than assuming socket inheritance is sufficient.
- Do not forward the agent to remote machines.
- Register cleanup as soon as the process starts, including failed key loading.
- Await/terminate owned dependent processes before stopping the agent. Cover
  success, failure, timeout, and cancellation; do not claim cleanup can run after
  uncatchable process termination. Use a bounded identity lifetime as additional
  protection and define orphan-process handling.
- Treat socket/cleanup handles as runtime-only capabilities, not durable state
  or reusable serialized workflow results.

Audit SDK resource-scope/finalizer support in Green, Red, and Blue before choosing
the API. Add a narrow scope abstraction if needed; a success-path cleanup step
does not satisfy the requirement. Avoid global environment mutation during
parallel execution.

## Destruction and migration

SSH resource deletion is a separate explicit operation; deleting compute nodes
or a fan-out never implicitly deletes the SSH resource.

Destroy all consuming machines and owned provider key registrations before
deleting their shared durable SSH resource. The SDK graph/caller must account for
all consumers; do not infer that a key is unused from one node's deletion.
Stopping an agent never deletes durable keys. Compute-only destruction should
not require unlocking a key unless a separate application cleanup step needs SSH.

Existing deployments require an explicit migration plan:

1. Preserve ownership records and stop competing writers.
2. Encrypt the existing key while preserving its public identity, or explicitly
   plan an access-key replacement; never silently substitute a new key.
3. Transfer key/object/registration ownership without duplicate management.
4. Review the resulting OpenTofu plan for unintended machine replacement or
   public-key changes before removing `tls_private_key.machine` from ownership.
5. Verify access and recovery through the new agent scope before retiring the
   previous access path.

Historical state versions, plans, and backups still contain the old plaintext
key. Encrypting the current representation does not remove that history. Document
retention implications and any separately authorized key rotation; do not erase
historical state automatically.

## Validation and implementation sequence

First settle the remaining authority/recovery protocol, lock implementation,
backend layouts, per-resource secret bindings, provider registration schemas, and
SDK scope/finalizer API. Registration ownership and one-agent-per-scope behavior
are agreed; their implementation still requires design and validation.
Then update contracts and implement equivalent behavior in all three colors,
keeping the existing API available alongside the new API. Integrate and validate
one explicit caller before migrating deployments individually.

Tests must include:

- Real encrypted `ssh-keygen` generation and `ssh-add` loading, including absent
  and wrong passphrases, unusual secret characters, and noninteractive execution.
- No plaintext private key or passphrase in files, plans/state, results,
  diagnostics, or process arguments; ciphertext persists and survives recovery.
- Idempotent reuse, interrupted creation, concurrent first creation, wrong
  fingerprint, missing authority, failed downloads, and passphrase rotation.
- Agent cleanup after all exit paths, failed loading, multiple identities,
  parallel compute failure, and isolated simultaneous workflow sessions.
- Local SSH/Ansible integration proving authentication through the scoped agent
  without a decrypted key file. Synthetic runners alone cannot prove this.
- Local workdir/profile placement, matching remote namespacing, backend inheritance
  and overrides, and stable resource-name isolation.
- Two independently passphrased SSH resources feeding separate compute fan-outs
  through one scoped agent, plus explicit reuse of one secret across resources.
- Shared-key deletion ordering, separate SSH deletion, provider registration
  ownership, and resource-scoped mutation locking.
- Three-color plan/lifecycle parity and migration plans without unintended
  machine replacement.

Run the repository checks documented in `README.md` and appropriate checks in
each changed SDK/caller repository. Live creates/deletes and state migration need
explicit authorization. Do not commit or push unless explicitly asked.

## References

- OpenSSH key generation and encrypted format: https://man.openbsd.org/ssh-keygen
- Key loading and askpass: https://man.openbsd.org/ssh-add
- Agent process and identity lifetime: https://man.openbsd.org/ssh-agent
- Ansible SSH connection: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/ssh_connection.html
