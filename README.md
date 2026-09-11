# Colors compute

Shared compute code for Green, Red, and Blue package skills. The target is one
single-node operation used directly by single-host packages and through Colors
fan-out by cluster packages.

The library implements shared and per-node create/delete operations, conditional
deployment ownership, SSH lifecycle, remote state access, and credential-free
planning in all three colors. Cluster operations use native Colors fan-out and
join. Eight VM cluster packages now consume the library. Existing deployments
still require explicit state migration; the address planner cannot transfer them.

All eight provider template sets have passed OpenTofu 1.12.5 schema validation.
The packaged `provider_plan` API loads those templates in each language.
This proves rendering and schema compatibility, not provisioning, network
isolation, backend locking, or safe migration of an existing deployment.

## Contract and implementation

- [Contract operations](contracts/README.md): provider selection, credentials,
  stable state keys, topology expansion, complete result collection, and state
  decisions that refuse mutation when ownership cannot be read.
- [Rendering and backend plans](contracts/rendering.md): typed JSON templates,
  S3/R2 configuration, and separate R2 backend credential bindings.
- [Read-only backend sessions](contracts/runtime.md): private credential/cache
  files, exact subprocess environments, strict state reads and failure refusal.
- [Coordination transitions](contracts/coordination.md): pure conditional-write
  plans for ownership and node attempts, with strict schemas and immutable results.
- [Conditional journal transport](contracts/object-transport.md): private AWS CLI
  sessions for confirmed missing keys and conditional S3/R2 writes.
- [Runtime coordinator](contracts/coordinator-runtime.md): serialized conditional
  writes and confirmation of ambiguous responses.
- [Lifecycle journal](contracts/lifecycle-journal.md): shared resources, nodes,
  key ownership, scale-down, deletion, and recreation.
- [Application requests](contracts/deployment-request.md): naming, external public
  key references, and deterministic builds.
- [Provider request resolution](contracts/provider-request.md): library-owned
  configuration bindings and firewall capabilities.
- [Managed S3 backend buckets](contracts/managed-backend.md): explicit ownership,
  bootstrap before state access, and disposal after full deployment retirement.
- [Deployment orchestration](contracts/orchestration.md): guarded operations,
  failed sibling settlement, and complete inventories.
- [OpenTofu execution](contracts/tofu-runtime.md): private saved plans, replacement
  refusal, and post-apply state checks.
- [Managed Kubernetes](contracts/managed-kubernetes.md): Vultr and DigitalOcean
  control planes, separate ownership, and private validated kubeconfig files.
- [Migration review](migration/README.md): an AutoMQ state-address inventory
  that emits no resource attributes and cannot execute a state transfer.

The registry declares eight target compute providers: Azure, AWS, Google,
DigitalOcean, hcloud, Vultr, Yandex, and OCI. A registry entry is not evidence
of completed provider lifecycle support. `no-infra` is refused. Backends are
R2 and S3; SMTP, DNS, and GitHub integrations remain outside this library.

## Language packages

| Color | Dependency location | Core API | Workflow API |
|---|---|---|---|
| Green | Git dependency with `:deps/root "green"` | `io.github.getcolors.compute` | `io.github.getcolors.compute-workflow/cluster-workflow` |
| Red | Root Git/npm facade `colors-compute-red` | main export | `colors-compute-red/workflow` |
| Blue | Python Git dependency with `subdirectory=blue`, distribution `colors-compute-blue` | `colors_compute` | `colors_compute.workflow.cluster_workflow` |

Consumers must pin an existing published commit. Green and Red pin their SDK
dependencies inside each package. Blue declares a normal SDK requirement; the consuming application must
supply an explicit Blue git pin. The Blue development group pins the SDK for
this repository's tests, without imposing that git source on consumers. Green,
Red, and Blue must be interchangeable for the same inputs and state identities.

The workflow constructor takes ordered expanded requests, an entry node id,
a standard Colors node step, and an optional downstream step. The node step
receives `colors-compute/request` and returns `colors-compute/params`. The join
validates and orders every result under `colors-compute/cluster`. A failed
branch prevents downstream execution. One-node workflows call the same step.
In Green these keys are keywords; engine error keys use the matching color.

Package lifecycle steps call `orchestrate`, which owns locking, SSH preparation,
shared resources, state access, node fan-out and the join. `plan_deployment`
provides build artifacts and documentation addresses without credentials or
local SSH access. `read_deployment` supplies recorded inventory for application
cleanup and inspection, refusing held journals and incomplete results.

Blue exports these names from `colors_compute`. Red exports snake-case planning
helpers and `orchestrate` from its main entry. Green exposes the operations in
`compute-orchestration`, `compute-planning`, and `compute-inspection` namespaces.
See the contracts for native argument names and injected test dependencies.

## Development checks

Requirements: Python 3.11+, uv, Babashka, and Bun. OpenTofu is needed for
provider schema checks, which use temporary directories and no credentials.

```sh
python3 scripts/registry.py
python3 scripts/provider_resources.py
python3 scripts/provider_recipes.py
uv run --project blue python scripts/parity.py
uv run --directory blue pytest -q
cd green && bb test
cd ../red && bun install --frozen-lockfile && bun test && bun run typecheck
```

The native backend probes use OpenTofu 1.12.5 and synthetic credentials only:

```sh
python3 scripts/backend_probe.py --tofu /path/to/tofu --blue-runner
python3 scripts/backend_http_probe.py --tofu /path/to/tofu --bb /path/to/bb --bun /path/to/bun
python3 scripts/journal_http_probe.py --aws /path/to/aws --bb /path/to/bb --bun /path/to/bun
```

The second probe starts a loopback-only S3 server, refuses writes, and exercises
each native reader. It verifies R2 credential selection even when ambient AWS
keys, session tokens and invalid profile selectors are present. The journal
probe uses HTTPS and a temporary trusted certificate to test conditional writes
with the actual AWS CLI in every color. It requires AWS CLI 2 with conditional
PutObject support, verified locally with 2.35.11, and OpenSSL.

Run the commands from the repository root except the explicit directory changes.
`BUN`, `BB`, and `PYTHON` may select executables for parity. Provider-specific
`check.py` files compare deterministic examples and accept `--tofu /path/to/tofu`
for backend-disabled initialization and schema validation. This is not live
cloud verification.

## Rollout order

Implement provider and shared lifecycle operations first, then migrate cluster
packages beginning with AutoMQ. Prove a role-based cluster before completing
the remaining cluster migrations. Single-host package migrations follow the
clusters. Existing cluster states require explicit shared/per-node ownership
transfers, backups, and reviewed plans before applying the new configurations.

The full plan is in the workspace repository at
`plans/colors-compute-implementation.md`; the five compute/SSH standards there
define the target. Foundation code must not be presented as completed rollout.

`power_deployment(opts, "start"|"stop", environment?, dependencies?)` powers an
owned singleton through the library. Green exposes `compute-power/power-deployment`.
OCI and Vultr are supported; other providers fail before mutations. The runtime
acquires an existing lifecycle lease, reads the immutable provider ID from owned
state, waits for the terminal state, and returns refreshed public IP metadata on
start. An uncertain mutation retains the lease for explicit recovery. It never
runs an OpenTofu apply or changes the VM's desired power state. See
[the power contract](contracts/power-runtime.md) for result and recovery semantics.
The test suite includes synthetic local HTTPS calls and OCI CLI input generation;
these checks do not prove live permissions or availability.

### GCS state backend

Select `provider-backend=gcs` with `gcs-bucket`, `gcs-region`, and
`google-project`. OpenTofu uses each logical state key as its GCS prefix, so
`demo/compute/shared.tfstate` stores state at
`demo/compute/shared.tfstate/default.tfstate`. The coordination journal uses
GCS generation preconditions for atomic writes.

Set `gcs-bucket-mode=managed` to create and retire the state bucket with the
compute lifecycle. The bucket has uniform access, public access prevention,
versioning, and disabled soft delete. Ownership labels and a marker bind it to
the project and profile. Deletion requires `compute-prevent-destroy=false`, a
retired compute journal, and empty package states. Cleanup removes all object
generations before deleting the bucket. External buckets are never deleted.

OpenTofu uses Google Application Default Credentials. Journal and bucket
operations use the active `gcloud` account, which must have access to the same
project. Enable the Cloud Storage API before bootstrapping the bucket.

## OCI state and bucket lifecycle

Select `provider-backend=oci` for an OCI-only deployment. The library creates
and deletes managed state buckets with the native OCI API. OpenTofu state uses
OCI's S3 compatibility endpoint, while journal compare-and-swap writes use the
native API because OCI's compatibility API accepted stale `If-Match` writes in
a live test. See [managed OCI backend buckets](contracts/managed-oci-backend.md)
for configuration, ownership checks, cleanup and explicit failed-node recovery.
