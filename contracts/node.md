# Public-identity compute and registration contract (v2)

This is a greenfield breaking replacement. Compute never generates, stores,
unlocks, downloads, or deletes a private SSH key. The SDK owns graph ordering:
SSH resource ready → provider registration and compute; application access also
waits for the scoped agent. See [SSH resources](ssh-resource.md).

## Compute API

`node_plan(opts, request)` is pure; `build_node` persists templates;
`compute_node(opts, request, operation, environment, dependencies)` executes
`build`, `create`, `inspect`, `resolve-connection`, or `delete`. Green exposes `node-plan`,
`build-node!`, and `compute-node!` in `io.github.getcolors.compute-node`.
There is no compute `prepare-access` operation.

Request fields are `node_id`, `state_filename`, absolute normalized `workdir`,
`security`, optional `network`, and mandatory `ssh_resource`:

```json
{"reference":"opaque durable SSH resource reference",
 "public_key":"ssh-ed25519 BASE64",
 "fingerprint":"SHA256:BASE64"}
```

The public blob must be an ED25519 wire-format key matching the fingerprint.
A `status` field is accepted for direct composition of a ready SSH result.
No private fields are accepted. Explicit references allow one identity to feed
many machines or distinct identities to feed separate fan-outs.

AWS, DigitalOcean, Hetzner Cloud and Vultr also require `ssh_registration`, the
ready result of the separately owned registration below. Its provider, SSH
resource reference and fingerprint must match. Other providers consume the public
key directly and reject a registration. A node never declares the registered
provider key object; it uses the registration's `id`.

The root lives at `<workdir>/<profile>/<node_id>`. Remote state is
`<s3-prefix>/<profile>/<state_filename>`; empty prefix omits its separator.
Local state is `<workdir>/<profile>/<node_id>/<state_filename>`.
`key_objects` is always empty. The compute state identity pins profile, node,
state filename, provider, SSH resource reference and fingerprint. Changing this
identity or an existing backend is refused. Provider migration requires a distinct
node identity. Input interpolation and path traversal are refused.

Plans have `status: planned`, directory, state key, empty key objects and documents.
Build returns the same with `status: built`. Runtime returns `status: ready`,
directory and normalized `params`; these contain machine outputs and no
`ssh_identity_file`. Agent access is a separate scope capability. Delete returns
`status: destroyed`. Errors follow [structured diagnostics](errors.md).

## Google C4A Local SSDs

For a C4A `standard` or `highmem` `-lssd` machine, explicitly set
`google-local-ssd-count` to the fixed count for its shape: 4 vCPUs → 1,
8 → 2, 16 → 4, 32 → 6, 48 → 10, 64 → 14, 72 → 16.
The node declares that many 375 GiB NVMe `scratch_disk` blocks, avoiding
unmanaged implicit disks and replacement drift. A missing or mismatched count
is refused; this option is currently limited to these C4A shapes. Formatting,
mounts and recovery of ephemeral data remain the application's responsibility.
No commitment or reservation is purchased by node creation.

## Provider registration API

`registration_plan`, `build_registration`, and `compute_registration` use the
same lifecycle arguments. Green uses `registration-plan`, `build-registration!`,
and `compute-registration!` in the same namespace.

The request is exactly `name`, `workdir`, `state_filename`, and `ssh_resource`.
The private OpenTofu root is `<workdir>/<profile>/registration-<name>`; its
independently supplied state filename uses the same profile namespace. Names
must keep the combined profile and registration node identity within 63 characters.

The root owns exactly one `aws_key_pair`, `digitalocean_ssh_key`,
`hcloud_ssh_key`, or `vultr_ssh_key`, plus its provider/backend configuration.
It owns no machine, network, firewall, or SSH private material. AWS registration
identity also pins its region. Provider credentials select the account/project;
callers must keep that account stable for a registration's lifetime.

A ready result contains `status`, `directory`, `reference` (the profile-qualified
state key), `provider`, `ssh_resource_reference`, `fingerprint`, and string `id`.
AWS's `id` is its key name, the value an EC2 instance consumes. Other providers
return their registered key identifier. The SDK graph must wait for this result
before creating consuming nodes. Use separate registrations for separate provider
accounts/projects/regions and give each one exclusive ownership.

## Persistent state and lifecycle guards

Build is credential-free. OpenTofu uses the persistent root and its default
`.terraform` directory. No redirected `TF_DATA_DIR`, automatic state import,
force-unlock, journals, or uncertain mutation retries are introduced. Backend
credentials are temporarily supplied in a private file and removed in cleanup.
SSH passphrase bindings are never forwarded to provider processes.

Native backend locks protect state mutation. The caller serializes operations
sharing a local directory, including builds and initialization. Files and
working directories are private and protected against symlink substitution.
Templates and initialization files remain after destruction.

Create validates state identity before planning. `compute-require-existing-state`
refuses absent ownership. Read failures are never interpreted as absence.
Create refuses delete/replacement actions; delete requires explicit
`compute-prevent-destroy: false` and refuses create/update actions. Delete cannot
proceed against independently missing state. Empty state with leftover outputs
requires recovery; a strictly empty readable state can be inspected as destroyed.
Cancellation propagates and owned command processes are cleaned up by the runner.

Delete nodes first, then their separately owned registrations, then explicitly
delete the durable SSH resource after all consumers are gone. Compute deletion
needs no passphrase and never deletes durable SSH authority or starts an agent.

## Command failure diagnostics

Lifecycle errors retain their stage-specific `code`, authored `message`, safe
`command` prefix, optional resolved `executable`, `exit_code`, and bounded,
redacted `stderr`. They never contain stdout, full argv, or raw exceptions.
The additive `command_reason` field distinguishes native runner failures:

- `executable_not_found`: no command candidate exists in the supplied PATH.
- `process_start_failed`: an existing candidate cannot execute, the working
  directory is invalid, or the operating system cannot start the process
  (including an unavailable script interpreter).
- `timeout`: the process or its captured output exceeded the deadline; the
  runner terminates its owned processes.

These failures retain `exit_code: -1` for compatibility. A child that actually
exits with a nonzero status, including 127, has no `command_reason`. Callers must
not infer missing executables from a negative status. Custom runners may omit
the field; unknown values are not forwarded. Execution resolves only the
supplied PATH, with relative and empty entries relative to the working
directory. An omitted PATH never inherits the parent process's search path.
Cancellation continues to propagate instead of becoming a command failure.

## Live connection resolution

`resolve_connection(opts, request, environment, dependencies)` (Green:
`resolve-connection!`) returns the usual ready result with freshly observed
normalized `params`, including the current address. It requires existing owned
node state and the same public SSH identity and registration request as compute.
Provider and backend credentials are required; no SSH passphrase is needed.

The resolver validates stored ownership, then uses an OpenTofu refresh-only
saved plan and JSON inspection to read the provider's current machine. In that
refresh-only JSON, `prior_state.values` contains the freshly observed resources;
`planned_values` contains the recomputed outputs and need not contain resources.
The resolver requires that refreshed snapshot and refuses errored plans. It never
applies a plan or persists refreshed remote state. It checks the machine's
immutable ID against owned state (Google's numeric instance ID and Azure's VM
UUID, rather than their reusable resource paths), validates output identity, and
refuses missing/replaced machines, unknown or incomplete outputs and provider
failures. Google and Azure public addresses must still be attached to the
observed owned machine; a detached or reassigned address is refused. It never
falls back to the stored address.

Initialization and rendering still use the persistent private local root; callers
must serialize it with other operations. The plan uses native backend locking
with a 60-second lock timeout. The private saved refresh plan is removed on all
exits because plan files can contain sensitive provider data. Remote backend
lock acquisition/release is the only remote write; no infrastructure or state
mutation is requested. Connection readiness is not an SSH reachability check.
