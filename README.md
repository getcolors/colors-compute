# Colors compute

One independently identified compute unit for Green, Red, and Blue. A unit owns
its machine, supporting provider resources, and SSH keys. Local backends keep
authoritative keys in local OpenTofu state; remote backends use S3-compatible
key objects. The caller and Colors SDK own topology, fan-out, joins,
ordering, scaling, and application migration.

## Public lifecycle

The new API is `node_plan(opts, request)`, `build_node(opts, request)`, and
`compute_node(opts, request, operation, environment, dependencies)`; Green uses
`node-plan`, `build-node!`, and `compute-node!` in `compute-node`.
See [the single-node contract](contracts/node.md) for exact inputs and lifecycle.

The SDK supplies `request.workdir`, `request.node_id`, and
`request.state_filename`. Each invocation operates in
`<workdir>/<profile>/<node_id>`. Build writes persistent Terraform templates.
OpenTofu executes in that directory with its default `.terraform/` folder.
The library never deletes templates or redirects `TF_DATA_DIR`.

For `s3-prefix: infrastructure`, `profile: production`, and filename
`app-0.tfstate`, remote state is `infrastructure/production/app-0.tfstate`.
The corresponding remote key objects are
`infrastructure/production/app-0/ssh-key` and `ssh-key.pub`.
Native OpenTofu state locking protects each unit. There is no deployment journal
or cluster coordinator.

Remote SSH keys are OpenTofu resources in the node state. The local keypair is a
disposable Ansible copy, always overwritten from remote storage before access.
It is never uploaded or adopted. Failed retrieval refuses access. Destruction
removes the machine before its remote key objects and removes local copies only
after successful infrastructure destruction. Templates and initialization files
remain. Remote state and saved plans contain secret key material and must be
protected as credentials. With the local backend, local state is the key
authority and access files are refreshed from its sensitive outputs. No S3
credentials or key objects are required.

## Migration

This is a breaking API and state-layout change. Existing package pins keep the
old implementation until explicitly updated. Do not point the new API at an
old shared/per-node deployment and apply: follow the [migration procedure](migration/README.md).
No live state, credentials, deployment configuration, or consumer pins are
changed by this repository update.

An AWS-to-OCI migration uses distinct unit identities and state filenames. The
caller creates OCI, transfers data and switches traffic, then explicitly deletes
AWS. Changing providers inside state that owns resources is refused.

## Provider support

Provider templates cover AWS, Azure, DigitalOcean, Google, Hetzner Cloud, OCI,
Vultr, and Yandex. Provider-neutral security and network requests are resolved
inside the library; callers do not supply arbitrary Terraform resource bodies.
Owned supporting resources belong exclusively to one unit. Existing network
references can be passed where a provider supports them.

State backend rendering supports S3, R2, OCI, GCS, and local storage. Authoritative
SSH key storage uses local state for the local backend and S3-compatible
object storage for remote backends. The single-node contract
specifies supported combinations and explicit key-store settings.

## Checks

```sh
python3 scripts/registry.py
python3 scripts/provider_resources.py
python3 scripts/provider_recipes.py
uv run --project blue python scripts/parity.py
uv run --directory blue pytest -q
(cd green && bb test)
(cd red && bun install --frozen-lockfile && bun test && bun run typecheck)
```

Tests use synthetic runners and fixtures. Provider schema checks initialize
OpenTofu without backend access. They do not establish live cloud permissions,
endpoint locking behavior, or application readiness.
