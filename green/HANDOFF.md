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

## Protected read-only backend sessions

Implemented `io.github.getcolors.compute-runtime/read-state` per
`contracts/runtime.md`, with two/three/four-argument arities for options,
state key, optional environment, and optional runner. Runners receive
`[argv directory exact-environment timeout-ms]`. The JSONL fixture driver now
supports `read_state_case` with ordered fake responses.

The session writes backend configuration and bound R2 credentials into a fresh
0700 directory with 0600 files. It preserves ambient AWS credentials, removes
injected TF/TOFU/COLORS_PAR settings, runs only init then state pull, and always
removes its private directory/cache. Nonzero, malformed, empty, or uncertain
state reads return only `{:status "error"}`. There is no absent inference.
Validated version-four states return params (or empty params for legacy state).
Params may contain sensitive state outputs and must not be logged; bound backend
credential substrings, including escaped JSON forms, cause refusal.

The current SDK cannot replace inherited process environments, so this boundary
uses native ProcessBuilder. The runner has one deadline covering execution and
output drains, tracks descendants for timeout/cancellation cleanup, and emits
no process diagnostics through the read-state API. Cancellation propagates only
after workspace cleanup. Process/machine termination can still leave private
files; no crash-cleanup or secure-erasure guarantee is made.

Validation: `bb test` passes 20 tests / 155 assertions. Tests inspect actual
permissions and private credential placement, retained ambient authentication,
fixed argv and sanitized environments, malformed/trailing JSON, state shape,
secret-output refusal, failures/cancellation cleanup, native exact-environment
replacement, and bounded output pipes inherited by a child process.
No cloud operations or credentials were used. This adds read-only backend IO;
backend absence observation, coordination, state writers, shared/node creation,
and package migration remain unimplemented.


Read-session follow-up: mathematical JSON integers such as version `4.0` and
serial `0.0` now match Red/Blue semantics; fractional/unsafe values still fail.
R2-only subprocess environments omit AWS_PROFILE and AWS_DEFAULT_PROFILE to
avoid ambient profile lookup blocking explicit R2 credentials. S3 retains
those selectors and ambient AWS access/session credentials remain unchanged.
`bb test`: 22 tests / 161 assertions pass. The all-color
`scripts/backend_http_probe.py` passes through actual native runners/OpenTofu
against synthetic loopback S3: private backend JSON accepted, correct R2 signing,
no ambient AWS key or token used, and GET/HEAD only. No live cloud access.

Native PATH follow-up: a real regression test reproduced Java ProcessBuilder
resolving executables from its parent's PATH instead of the supplied environment.
The runner now resolves argv[0] explicitly from the exact child PATH, with no
fallback to the parent. Absolute/relative executable paths remain explicit.
A temporary fake tofu and missing-executable test now pass; `bb test` totals
23 tests / 163 assertions. The all-color loopback backend probe still passes.

## Pure coordination reducer

Implemented `io.github.getcolors.compute-coordination/coordination` and the
`coordination` JSONL fixture operation. It validates strict identity/event/
observation/journal schemas in the specified error order, discovers providers
from the packaged registry, and returns conditional-write intentions for
acquire, declare, start, complete, fail, and release. Nodes keep derived state
keys and stable IDs; topology and operation records are preserved across
release/reacquire. Input values remain immutable. Numeric JSON integers are
interpreted mathematically, while booleans/fractions/unsafe ranges are refused.

Validation: full `bb test` passes 26 tests / 283 assertions, including all current
shared coordination fixtures, immutable multi-node transition history, rejection
of secret extension fields, and unsafe JSON node-key rejection. No coordinator
transport, CAS execution, distributed-lock evidence, node dispatch, provider
lifecycle, retry/recovery takeover, or package migration is implemented here.
A returned intention never grants permission to dispatch before CAS confirmation.
