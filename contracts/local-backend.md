# Local state backend

Select `provider-backend: local`. When `local-state-dir` is omitted, it defaults
to `$HOME/.local/state/colors`. The process environment supplies `HOME`, so the
default stays the same across working directories. Profiles retain their own
subdirectories. Set `local-state-dir` to an absolute, normalized POSIX directory
to override the default. The directory must not contain empty, `.` or `..`
components, backslashes, or NUL characters. A trailing slash is allowed only
for `/`. No backend credentials or cloud storage tools are required.

```yaml
profile: demo
provider-backend: local
```

Compute provider credentials are still required for provider operations.
Build and planning resolve the home directory and validate paths without
creating directories or reading state. An explicitly blank, null, or invalid
override is an error. If `HOME` is missing or invalid, specify `local-state-dir`.

## State layout

`backend_plan(opts, state_key)` renders the OpenTofu `local` backend with
`path` equal to `<local-state-dir>/<state_key>`. For example, the shared state
above, with `HOME=/home/operator`, lives at `/home/operator/.local/state/colors/demo/compute/shared.tfstate`.
Node states, managed Kubernetes state, and package state retain their existing
logical keys under that directory.

OpenTofu runs in private temporary working directories, but the state paths
are absolute and persist after cleanup. The runtime creates missing state
directories with mode `0700` and protects state and backup files with mode
`0600`. It rejects symlinks and non-regular state files. Only a missing file
proves absence. Permission failures, unreadable paths, and malformed state
do not authorize creation over existing resources.

OpenTofu supplies its native per-state lock. The library uses a separate
journal for deployment ownership and lifecycle coordination. See the
[OpenTofu local backend documentation](https://opentofu.org/docs/language/settings/backends/local/).

## Conditional journal writes

The backend identity is `{"kind":"local","path":"<local-state-dir>"}`.
Each profile stores its journal at
`<local-state-dir>/<profile>/compute/coordination.json`.
The ETag is the lowercase SHA-256 digest of the exact file bytes. Green, Red,
and Blue use the same path and digest, so each can continue a deployment
recorded by another color.

A writer creates the sibling directory `coordination.json.lock` atomically.
While holding it, the writer reads the current journal, checks `if_match` or
`if_none_match`, writes a private temporary file, and atomically renames it
over the journal. A competing writer returns `conflict`. A stale ETag never
authorizes a write. Readers see a complete old or new journal. Reads reject
invalid UTF-8, malformed JSON, non-object documents, and files over 2 MiB.

The writer removes its lock on normal completion or handled failure. A killed
process can leave the lock directory behind. There is no automatic timeout or
takeover. Before removing a leftover lock, confirm that its writer has stopped
and review the journal. The journal's lifecycle lease is separate and retains
the existing recovery rules.

## Operating limits

Use a filesystem on one host that supports atomic directory creation and
same-directory rename. This implementation does not establish cross-host
locking on network filesystems or guarantee survival of machine power loss.
Keep the state directory private and back it up with the journals and state
backups. It can contain private keys and sensitive provider data.

Deleting compute resources retains local state and the retired ownership
journal. Changing `local-state-dir` selects a different backend identity.
Selecting `local` does not migrate an existing remote backend. Existing
deployments still require an explicit, reviewed state transfer.
