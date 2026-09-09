# Green compute foundation handoff

Implemented the six pure operations in `contracts/README.md` in
`io.github.getcolors.compute`, with a packaged copy of the canonical registry.
The JSONL driver is `scripts/contract-green.clj`. `deps.edn` pins Green SDK
`3f33f5d4dcce1f8d97a11b6972e13eb3f0b37654`; `bb.edn` resolves that manifest.

Validation: `cd green && bb test` passed 5 tests, 27 assertions, including
a fresh run resolving the pinned dependencies. The topology/error JSONL
smoke check passed.

This is a pure contract foundation only. Provider lifecycle adapters, remote
state IO, shared ownership, SDK orchestration, provider templates, migration,
and package adoption remain outstanding. No cloud resources were created.

## Green SDK workflow adapter

`io.github.getcolors.compute-workflow/cluster-workflow` accepts
`[requests entry-node-id node-step]` or
`[requests entry-node-id node-step downstream]` and returns an ordinary Green SDK workflow.
`requests` is the ordered expansion result, with additional node configuration
allowed. `node-step` is a standard SDK `opts -> opts` step. It reads its
individual request from `:colors-compute/request` and returns normalized node
parameters at `:colors-compute/params`. It may throw or set
`:green/exit`/`:green/err` to report failure. Optional downstream is a standard
step invoked after successful collection.

Run it with `green.workflow/run`, or embed with `green.workflow/step`.
Successful collected data is under `:colors-compute/cluster`; per-branch input
and output use `:colors-compute/request` and `:colors-compute/params`.
The adapter uses actual SDK dynamic fan-out and join, including the single-node
case. It clears stale parent branch and collected result data at dispatch.

Validation: `bb test` now passes 11 tests / 52 assertions against the immutable
SDK dependency. Integration tests exercise actual concurrent branches finishing
in reverse order, complete metadata collection, the same single-node callback,
explicit and thrown failures skipping a downstream composed step, and malformed
node outputs preventing a successful join.

The callback remains a seam for the common library node lifecycle, which has
not been implemented. This is orchestration evidence only, not cloud-provider
or package adoption evidence. No cloud calls were made.

## Rendering and backend planning

`io.github.getcolors.compute/render-template` and `backend-plan` implement
`contracts/rendering.md`, exposed as `render_template` and `backend_plan` by
the JSONL driver. Rendering preserves whole-value JSON types, leaves Terraform
expressions intact, rejects partial placeholders, and leaves keys untouched.
Backend planning validates selection, required fields, and the state path in
order. R2 returns credential binding names for protected partial backend
configuration, never AWS environment overrides or credential values. S3 retains
the ambient AWS chain. The functions perform no remote IO or environment reads.

Validation: `bb test` passes 13 tests / 83 assertions. JSONL smoke checks verify
false-value rendering, Terraform expression preservation, and R2 planning that
does not echo a supplied secret. Runtime binding/cache protection, actual
backend initialization, lock behavior, and lifecycle execution remain pending.

## Packaged provider planning update (2026-09-09)

Implemented `compute/provider-plan` using the classpath resource
`colors_compute/templates.json` and existing `render-template`; no working-tree
provider lookup is used. Exposed JSONL `provider_plan` in the Green driver.
Tests cover actual Vultr node/shared-keygen documents, typed lifecycle values,
preserved OpenTofu expressions, missing provider/stage and missing inputs.
`bb test` passed: 14 tests, 90 assertions. Driver selection diagnostics also
passed from `/tmp`, outside the repository working directory. This remains
pure rendering and makes no runtime provisioning claim. Template resource
packaging and cross-color bundle generation remain owned by the parent task.
