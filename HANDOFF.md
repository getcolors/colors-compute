# Implementation handoff

Updated 2026-09-09. Work is active and the implementation is incomplete.

## Authorization and scope

The user authorized implementation, subagents, commits and pushes to main,
and live deployments if needed. Cluster package migrations precede single-host
migrations. Before stopping, update this handoff and the workspace handoff.

## Completed foundation

- Three-color pure contract cores and pinned SDK dependencies.
- Actual Colors SDK fan-out and join adapters in all three languages.
- Pure template rendering and S3/R2 backend planning with no credential values
  in generated configuration and no R2 overrides of ambient AWS credentials.
- Common fixture drivers with 99 passing cross-color cases and an intentional
  regression check of the comparator.
- Read-only AutoMQ state migration planner, with explicit node mappings.
- Provider template work in progress. Consult each provider README for exact
  schema checks; do not infer runtime support from registry entries.

## Validation and outstanding work

Latest foundation checks passed: Blue 38 tests; Green 13 tests / 83 assertions;
Red 20 tests / 94 assertions and typechecking; 99 cases per color in shared
parity; 7 migration-planner tests; packaged registry consistency. Provider
schema validation is credential-free, not a live test.

Still required: protected backend credential binding and cache handling,
remote state read/absence distinction, deployment coordination, ownership
records, shared SSH lifecycle, provider preflight, node apply/destroy, complete
network capability validation, scale-down/retry cleanup, and package migrations.
No existing package or deployment has been changed to use the new library.

Validated template prototypes: Vultr 2.32.0, DigitalOcean 2.51.0, hcloud 1.54.0,
AWS 6.31.0, Google 6.0.0, and Azure 4.30.0, using OpenTofu 1.12.5. Yandex and OCI are under
active agent implementation and must not be claimed verified before their
schema checks finish. Provider READMEs record limits, including hcloud private
network filtering and pending MySQL reserved-IP integration.

## Published work and live resources

Workspace standards and plan: `873f384`, pushed to `getcolors/workspace/main`.
GitHub repository `getcolors/colors-compute` exists. This foundation is being
published to main. The Blue wheel was built and smoke-tested outside the
checkout; its metadata now carries the immutable Colors SDK Git dependency.
Using only a uv development source mapping had resolved the unrelated PyPI
package in the first packaging test; that defect was fixed before publishing.
No live cloud resources have been created, changed, or destroyed by this task.

Unrelated workspace changes to `scripts/package-copies.py`, `card-prompt.md`,
and `scripts/__pycache__/` must remain excluded from task commits.
