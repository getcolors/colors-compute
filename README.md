# Colors compute

Shared compute code for Green, Red, and Blue package skills. The target is one
single-node operation used directly by single-host packages and through Colors
fan-out by cluster packages.

This repository is under implementation. It currently provides executable
contracts, SDK cluster workflow composition, pure template/backend planning,
provider template prototypes, protected read-only backend sessions, and a
read-only AutoMQ migration planner. It
does not yet provide a production create/delete lifecycle. No package or live
deployment has migrated to it.

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
- [Coordination design](contracts/coordination-design.md): remaining deployment
  ownership and crash recovery requirements before any provider mutation.
- [Migration review](migration/README.md): an AutoMQ state-address inventory
  that emits no resource attributes and cannot execute a state transfer.
- [Handoff](HANDOFF.md): current evidence and unfinished work.

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

Consumers must pin an existing published commit. The SDK dependencies are
pinned inside each package. Green, Red, and Blue must be interchangeable for
the same inputs and state identities.

The workflow constructor takes ordered expanded requests, an entry node id,
a standard Colors node step, and an optional downstream step. The node step
receives `colors-compute/request` and returns `colors-compute/params`. The join
validates and orders every result under `colors-compute/cluster`. A failed
branch prevents downstream execution. One-node workflows call the same step.
In Green these keys are keywords; engine error keys use the matching color.

This constructor composes node operations. It does not authorize a caller to
skip deployment locking, SSH preparation, shared-resource ownership, or remote
state initialization. Those production lifecycle operations remain unfinished.

## Development checks

Requirements: Python 3.11+, uv, Babashka, and Bun. OpenTofu is needed for
provider schema checks, which use temporary directories and no credentials.

```sh
python3 scripts/registry.py
python3 scripts/provider_resources.py
python3 scripts/parity.py
uv run --directory blue pytest -q
cd green && bb test
cd ../red && bun install --frozen-lockfile && bun test && bun run typecheck
```

The native backend probes use OpenTofu 1.12.5 and synthetic credentials only:

```sh
python3 scripts/backend_probe.py --tofu /path/to/tofu --blue-runner
python3 scripts/backend_http_probe.py --tofu /path/to/tofu --bb /path/to/bb --bun /path/to/bun
```

The second probe starts a loopback-only S3 server, refuses writes, and exercises
each native reader. It verifies R2 credential selection even when ambient AWS
keys, session tokens and invalid profile selectors are present.

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
