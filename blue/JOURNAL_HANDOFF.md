# Blue journal transport handoff

Implemented 2026-09-09: public async `journal_get(opts, environment=None,
runner=None)` and `journal_put(opts, intent, environment=None, runner=None)`.
Injected runners receive argv, private working directory, exact child
environment and timeout_ms, returning the existing ProcessResult shape.

The implementation derives the coordination object key, validates write intents
through the existing strict reducer document validator and selected identity,
limits bodies to 2 MiB, and uses exactly one AWS CLI conditional operation.
R2 credentials live only in a 0600 private shared-credentials file under a 0700
temporary directory; its child environment isolates AWS settings except trusted
AWS_CA_BUNDLE. S3 retains the ambient chain. Parent environments are unchanged.
Only exact anchored operation-specific service errors classify absence/conflict;
other failures are generic and do not prove write failure. Cancellation propagates
after temporary-directory cleanup; subprocess process-group handling reuses the
read-only backend runner.

Tests: 40 journal unit cases passed; full Blue suite 170 passed. Cases cover
private files and credentials, source environment preservation, both conditions,
strict malformed/nested-secret inputs, selected identity mismatch, oversized
read/write, exact error-prefix classification, ambiguous timeout/result behavior,
no write retries and exception/cancellation cleanup.

Parent owns actual AWS CLI loopback signing/CAS integration across colors. No
live service calls ran in this subtask. This transport does not implement lost
response readback, coordinator ownership, dispatch, or any cloud lifecycle.

AWS CLI 2.35.11 probe follow-up: service-error parsing now accepts its optional
exact `aws: [ERROR]: ` prefix and `(reached max retries: N)` suffix while
retaining legacy CLI formatting. Modern NoSuchKey/PreconditionFailed cases and
misleading prefix/message/wrong-operation regressions pass: 45 journal tests.

Parity-hardening follow-up: body reads now strictly decode UTF-8 and reject BOM,
UTF-16 and invalid bytes. Known R2 credential values, including JSON-escaped
forms, are rejected from serialized intents/command arguments before execution
and returned ETags/documents before return. Targeted journal suite: 56 passed.
No native CLI or full unchanged suite was repeated for this focused change.
