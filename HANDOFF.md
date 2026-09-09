# Implementation handoff

Updated 2026-09-09. The provider-plan milestone is published; the production
lifecycle and package rollout remain incomplete.

## Authorization and scope

The user authorized implementation, subagents, commits and pushes to main,
and live deployments if needed. Cluster package migrations precede single-host
migrations. Before stopping, update this handoff and the workspace handoff.

## Completed foundation

- Three-color pure contract cores and pinned SDK dependencies.
- Actual Colors SDK fan-out and join adapters in all three languages.
- Pure template rendering and S3/R2 backend planning with no credential values
  in generated configuration and no R2 overrides of ambient AWS credentials.
- Common fixture drivers with 129 passing cross-color cases and an intentional
  regression check of the comparator.
- Read-only AutoMQ state migration planner, with explicit node mappings.
- Packaged template loaders in each color, generated from canonical provider
  assets. All eight providers have schema-validated examples; OCI stages
  explicitly compose the node document with its required image fragment.

## Validation and outstanding work

Latest foundation checks passed: Blue 41 tests; Green 14 tests / 90 assertions;
Red 24 tests / 109 assertions and typechecking; 129 cases per color in shared
parity; 7 migration-planner tests; packaged registry consistency. Provider
schema validation is credential-free, not a live test.

Still required: protected backend credential binding and cache handling,
remote state read/absence distinction, deployment coordination, ownership
records, shared SSH lifecycle, provider preflight, node apply/destroy, complete
network capability validation, scale-down/retry cleanup, and package migrations.
No existing package or deployment has been changed to use the new library.

Validated template prototypes: Vultr 2.32.0, DigitalOcean 2.51.0, hcloud 1.54.0,
AWS 6.31.0, Google 6.0.0, Azure 4.30.0, Yandex 0.120.0, and OCI 8.4.0, using
OpenTofu 1.12.5. Provider versions are pinned in the canonical templates.
Provider READMEs record limits, including hcloud private
network filtering and pending MySQL reserved-IP integration.

## Published work and live resources

Workspace standards and plan: `873f384`, progress handoff `1f169b7`, pushed to
`getcolors/workspace/main`. Library foundation `a112e17` was pushed to
`getcolors/colors-compute/main` and its GitHub Checks passed. Packaged templates,
OCI/Yandex assets, and review fixes were pushed as
`1cf448db814773e56d9f7afb89bd814574c8f6c1`. Its contracts job and all eight provider
schema jobs passed in [GitHub Checks](https://github.com/getcolors/colors-compute/actions/runs/34353691140).
The Blue wheel was built and smoke-tested outside the
checkout; its metadata now carries the immutable Colors SDK Git dependency.
Using only a uv development source mapping had resolved the unrelated PyPI
package in the first packaging test; that defect was fixed before publishing.
Review also caught stale Red workflow results and JavaScript array coercion
in provider selection. Both are fixed with regression coverage. Missing node
outputs cannot reuse previous params, and all colors reject array selectors.
Published Git dependency smoke tests against `1cf448d` passed for all three
colors in temporary directories outside the workspace. They imported both the
workflow API and packaged Vultr plans using the published dependency, not local
source overrides. Blue's installed wheel smoke test also passed.
No live cloud resources have been created, changed, or destroyed by this task.

Unrelated workspace changes to `scripts/package-copies.py`, `card-prompt.md`,
and `scripts/__pycache__/` must remain excluded from task commits.

## Next implementation task

Implement the real backend/state layer and deployment ownership coordinator
before enabling provider mutations. Resolve protected R2 credential binding,
cache redaction, confirmed-absence reads, multi-state deployment serialization,
and crash-safe ownership records. Then implement shared SSH/network preparation
and the common node apply/destroy step. Current workflows accept an injected
node callback; they are not a substitute for that lifecycle.

Use AutoMQ as the first package migration after those checks pass. Preserve
broker identities and review the planner's state mapping against actual plans.
Finish cluster packages before beginning single-host package migrations. The
full version-only provider-adoption proof remains outstanding, because no real
consumer has migrated yet. Do not mark the five-stage plan complete.

## Read-only runtime progress

The next slice adds native read-only backend sessions in all three colors.
It resolves R2 credentials from the provided environment into private partial
backend configuration, isolates the OpenTofu cache, and removes it on exit.
S3 retains ambient AWS authentication. R2 removes AWS profile selectors only
from its separate backend child process, preserving parent and compute settings.
Fixed init/state-pull commands require exit zero; malformed or empty state
returns a generic error, never absence. Known backend credential echoes are
rejected, including escaped strings. Params can otherwise contain sensitive
state outputs and must not be logged.

Tests exercise real child environments, temporary file permissions, cleanup,
and descendant timeout handling. OpenTofu 1.12.5 local probes establish that
missing local state can return exit zero with empty stdout. A loopback S3
endpoint confirms explicit R2 signing ignores ambient AWS session tokens.
These tests do not establish remote absence or real R2/S3 contention behavior.
The runtime bypasses existing SDK subprocess helpers because they cannot all
remove inherited environment controls. The SDK review records further timeout
and diagnostic issues; those SDKs were not changed by this slice.

Deployment coordination, object-storage transport, mutation lifecycle and all
consumer migrations remain unfinished. The ownership proposal is in
`contracts/coordination-design.md`. Cluster packages still precede single-host.

## Coordination transition progress

The pure `coordination` function now plans conditional journal writes in all
three colors. Strict schemas reject unreviewed fields. Acquisition refuses a
held lock; declaration records the full topology; node start records an attempt;
completion/failure retains sibling records; release refuses running attempts.
It checks identity, ETag, run ownership, write IDs and revision bounds. Results
are detached from caller input. No returned plan authorizes dispatch until an
object-store writer confirms its conditional write committed.

The schema intentionally does not yet implement shared key phases, retry,
scale-down, destruction, recovery takeover or tombstones. Transport and runtime
coordination remain required. The next concrete step is a protected conditional
S3/R2 object transport with confirmed NoSuchKey classification, then a coordinator
that serializes journal writes and handles lost responses before provider work.
The mutation lifecycle and every cluster/single-host consumer remain pending.

Review also found Red's regex end anchors accepted trailing newlines where the
other colors refused them. Identifier, role, state-key and template matching now
require the complete string. These fixes have regression coverage. A deterministic
mutation audit compared 4,608 malformed/altered coordination inputs across all three colors without a mismatch; explicit common fixtures remain the correctness
oracle. The audit script is temporary and is not a substitute for shared tests.

Current local checks pass: Blue 130 tests; Green 26 tests / 283 assertions;
Red 89 tests / 309 assertions and typechecking; 211 explicit parity cases per
color, including 54 coordination fixtures; registry and packaged template
consistency. The all-color native OpenTofu loopback probe passes with only
GET/HEAD requests. CI now runs the local-state and loopback probes. These
changes have not migrated a package or accessed a live cloud deployment.
