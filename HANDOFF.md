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
`getcolors/colors-compute/main` and its GitHub Checks passed. This follow-up
publishes packaged templates and the OCI/Yandex assets; check its CI result.
The Blue wheel was built and smoke-tested outside the
checkout; its metadata now carries the immutable Colors SDK Git dependency.
Using only a uv development source mapping had resolved the unrelated PyPI
package in the first packaging test; that defect was fixed before publishing.
Review also caught stale Red workflow results and JavaScript array coercion
in provider selection. Both are fixed with regression coverage. Missing node
outputs cannot reuse previous params, and all colors reject array selectors.
No live cloud resources have been created, changed, or destroyed by this task.

Unrelated workspace changes to `scripts/package-copies.py`, `card-prompt.md`,
and `scripts/__pycache__/` must remain excluded from task commits.
