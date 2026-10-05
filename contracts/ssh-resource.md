# SSH resources (greenfield API v2)

The SDK owns the graph. A named SSH resource returns only `reference`,
`public_key`, and `fingerprint` (plus `status`). Compute consumes that public
identity and an explicitly owned provider registration where required. Deleting
compute never deletes the SSH resource. This replaces per-node TLS ownership;
it does not reinterpret or migrate existing state.

## Address and backend

`ssh_plan(opts, request)` / Green `ssh-plan` accepts `name`, absolute `workdir`,
`passphrase_env` (a `COLORS_PAR_*` binding), and optional `backend` overrides.
Remote fresh creation also requires the per-operation `verified_absent` boolean
attestation described below; it does not alter the resource reference.
The profile always comes from opts. Backend overrides merge over workflow opts.
The resource directory is `<workdir>/<profile>/ssh/<name>`; local authority is
`resource.json` there. Remote authority is one JSON object at
`<s3-prefix>/<profile>/ssh/<name>/resource.json`, in the inherited bucket (or
explicit override). S3, R2, OCI-compatible S3 and GCS use their native conditional
object writes. A backend must implement atomic conditional writes; no fallback
to an unconditional write is permitted. Reference is canonical JSON of v2,
backend location, profile and name; it excludes workstation paths for remote
resources. Local references include the absolute authority path.

The single authority record contains the encrypted OpenSSH key, public identity,
KDF policy and ownership. There is no separate object/state pair to reconcile.
Resource data does not enter compute state. Files are private; decrypted private
keys are never written. Python 3.9+ and OpenSSH are process-adapter prerequisites in all
colors: one packaged, byte-checked process adapter implements crypto and storage
semantics, with native color APIs. This follows the existing endpoint adapter
packaging model and avoids three divergent implementations of secret handling.

## Serialization and recovery

Local operations hold a nonblocking OS advisory file lock; remote operations
conditionally replace the authority record using its ETag/generation. An absent
record is reserved conditionally before generation. The reserved record has a
random lock token and preserves any previous encrypted identity. Locks have no
lease or automatic expiry. A contender fails busy; it never steals a lock.
Access preparation takes the same lock as creation, rotation and deletion.

This is an explicit v2 contract decision superseding the old prohibition on
conditional ownership records. The record is the resource authority itself,
not an additional deployment journal. Failed reads never establish absence.
An interrupted first create retains its reservation. Recovery never generates a
replacement: `recover` requires the exact lock token, stopped competing writers,
and an existing encrypted bundle whose public identity can be verified with the
passphrase. An interruption before the bundle was persisted requires an explicit
operator decision to abandon that resource name; automatic recreation is refused.
Uncertain remote writes leave the record for read/recovery, never blind retries.
Deletion retains a ciphertext-free tombstone, preventing accidental name reuse.
A supplied expected reference/fingerprint must match. Missing authority with an
expected identity is an error. Removing authority and all ownership history
outside this API cannot be detected by a fresh caller; preserve backend history.

## Secrets and lifetime

Passphrases are resolved only at runtime, separately for every resource. Explicit
reuse of a binding is supported. Empty values, ASCII control characters, DEL,
and values over 1000 UTF-8 bytes are refused before reservation. This prevents
terminal editing, signal characters, and OpenSSH input-buffer truncation. Generation uses encrypted ED25519 OpenSSH format and
bcrypt KDF with 64 rounds. Rotation is explicit with old and new bindings,
preserves the public key, and atomically publishes the new encrypted bundle.
Changing a binding alone cannot rotate or regenerate keys.

Generation and rotation use controlled PTY prompts with terminal echo disabled;
the child receives no passphrase environment or arguments. The parent kills the
child before closing its terminal on failure, preventing EOF from becoming an
empty passphrase. Unlocking uses a single-attempt askpass child environment,
never command arguments or helper source. One dedicated agent per caller scope has a private
socket and bounded identity lifetime (default 900 seconds). Loaded identities
are fingerprint-checked. A disposable public-only `identity-<SHA256-of-reference>.pub` cache in each
resource directory selects individual keys with
`IdentitiesOnly=yes`, explicit `IdentityAgent`, and `ForwardAgent=no`.
The caller registers agent cleanup immediately through the SDK resource scope;
owned process cleanup precedes agent termination. The adapter renews loaded
identities before their lifetime expires only while
the caller pipe remains open; long sync operations therefore retain access. It
exits on pipe EOF or SIGTERM and reaps the owned agent. SIGKILL cannot run
cleanup; bounded key lifetime limits an orphan agent's authority. No global environment or operator
agent is changed. Handles are runtime capabilities and must not be serialized.

## Operations

`ssh_resource(..., operation, environment)` supports `create`, `inspect`,
`rotate`, `delete`, and `recover`. `create` reuses ready authority without changing
identity. Creating an absent remote authority requires `verified_absent: true`
after caller verification; see "Verified fresh creation with remote storage".
`inspect` never unlocks it. `rotate` takes `new_passphrase_env` in the
request. `delete` requires `allow_delete: true` and caller confirmation that all
machines and registrations were destroyed (`consumers_destroyed: true`). These
are explicit caller attestations, not inferred from one node. No passphrase is
needed for deletion. `recover` takes `lock_token`.

The agent resolves fresh authority using each resource request, validates the
expected reference and fingerprint, loads it, and releases the resource lock.
Concurrent rotation therefore serializes key loading; already loaded identities
continue working because rotation does not change the public key.

The public identity cache is refreshed from authority under the resource lock,
never adopted as authority, and removed after explicit SSH deletion. It can
safely remain in an SSH alias after the agent exits; that alias still requires
an explicitly selected live agent. Rotation may require active scopes to restart
with the new binding before their next identity renewal.

Public caches are separated by a hash of the full resource reference, including
backend overrides, so concurrent scopes cannot overwrite another resource's
identity selection file. For local-backed resources, local lock-file evidence
or a known public cache refuses recreation when authority disappears. For remote
resources, remote authority and caller provider verification govern existence;
local caches never establish or veto existence. Callers
on a new workstation must supply `expected` when retrieving an existing identity.

## Verification and references

`ssh_resources.py` checks all packaged adapter copies. `ssh_parity.py` compares
native plans, while `ssh_lifecycle.py` creates with Green, reads and loads with
all three colors, rotates with Red, and deletes with Blue using real OpenSSH.
`ssh_benchmark.py` measures the explicit KDF policy; on the implementation Mac,
generation took 0.50–0.60 seconds and loading 1/2/4 keys took 0.82/1.43/2.60 seconds.
These are observations, not performance guarantees.

The controlled prompt and key format follow [OpenSSH ssh-keygen](https://man.openbsd.org/ssh-keygen),
and loading/lifetime follow [ssh-add](https://man.openbsd.org/ssh-add). Remote
mutations require [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html);
[Cloudflare R2 supports conditional PutObject](https://developers.cloudflare.com/r2/api/s3/api/).
Backend endpoints without these semantics are unsupported; there is no
unconditional fallback. Cloud providers other than the recorded live Alice
DigitalOcean/R2 run are covered by plans and synthetic lifecycle checks, not
a claim of live end-to-end verification.

### Explicit encrypted local export

`ssh-export!` (Green) / `ssh_export` (Red, Blue) accepts
`(opts, request, destination, operation, environment)`; operation defaults to
`install`. The destination is a normalized absolute private directory owned by
this export, not a key filename. It contains encrypted OpenSSH `identity`,
`identity.pub`, and `ownership.json`, all mode 0600, in a mode 0700 directory.
No private key bytes appear in the result or protocol output. The passphrase is
not needed to copy the encrypted authority; ordinary OpenSSH asks for it later.

`install` reads ready authority, checks expected identity when supplied, and
refuses unowned, modified, symlinked, hardlinked or non-private destinations.
The manifest binds the backend resource reference, fingerprint and file hashes.
Repeated installs refresh ciphertext after passphrase rotation for the same
fingerprint. Rotation journals old/new hashes around an atomic key replacement,
so interruption leaves a verifiable export. A changed fingerprint requires
explicit removal first. Initial publication atomically refuses collisions on
Linux/macOS. Operations serialize through a lock on the private parent directory.

`inspect` and `remove` require no backend/provider credentials or passphrase and
never touch remote authority. Missing destinations return `status: absent`.
Inspection returns `status: installed`, `reference`, `fingerprint`, `public_key`,
`private_key_file`, and `public_key_file`. Removal validates ownership and content,
then atomically withdraws the directory before deleting its files, returning
`status: removed`. A process killed during final deletion can leave encrypted
files in a private `.ssh-export-remove-*` tombstone in the parent directory.

## Missing authority diagnostics

An `inspect` lookup that successfully confirms no authority exists returns
`status: "error"` with `error.code: "ssh_authority_missing"` and the authored
message `SSH authority missing`. Callers can identify the expected location and
explain how to restore the original encrypted authority. This code does not
prove there are no existing consumers and never authorizes generating a new key.
Backend read failures, denied access, malformed records, and non-ready records
remain `ssh_resource_failed`; they must not be interpreted as confirmed absence.
No credential values, encrypted records, or arbitrary exception text are emitted.

## Verified fresh creation with remote storage

Creating an absent remote-backed SSH authority requires the request boolean
`verified_absent: true`. The caller must first successfully inspect the remote
compute and registration records and verify directly with the provider that no
consuming machines or public-key registrations exist. Failed reads, uncertain
provider results, and inconsistent state do not establish absence. This is a
per-operation attestation from the owning workflow, not a persisted desired-state
option or a replacement for provider checks. Existing consumers without their
records require explicit recovery of the original authority.

A remote-backed public selector and local runtime lock are disposable cache;
they neither prove deployment existence nor veto a verified fresh creation.
Existing remote records still govern ready, locked, deleted, and expected-identity
checks. The authority is reread and reserved by conditional write; the attestation
does not bypass a concurrent authority write. Local-backed resources retain their
local record, lock, and cache recovery guards. No automatic key replacement,
state adoption, or provider cleanup is introduced.
