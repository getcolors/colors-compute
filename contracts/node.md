# Public-identity compute and registration contract (v2)

This is a greenfield breaking replacement. Compute never generates, stores,
unlocks, downloads, or deletes a private SSH key. The SDK owns graph ordering:
SSH resource ready → provider registration and compute; application access also
waits for the scoped agent. See [SSH resources](ssh-resource.md).

## Compute API

`node_plan(opts, request)` is pure; `build_node` persists templates;
`compute_node(opts, request, operation, environment, dependencies)` executes
`build`, `create`, `inspect`, or `delete`. Green exposes `node-plan`,
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
