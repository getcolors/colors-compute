# Conditional journal transport

Next implementation contract, 2026-09-09. Use AWS CLI 2 with GetObject and
conditional PutObject support, verified locally with 2.35.11. The library owns
this shared backend dependency. Consumer adoption adds the generic prerequisite
once; adding a compute provider must not add package-specific transport code.
The CLI supplies the ambient AWS credential chain for S3. Do not implement a
second credential-chain resolver or custom SigV4 signer.

The transport exposes `journal_get(opts, environment, runner)` and
`journal_put(opts, intent, environment, runner)`, with optional environment and
runner parameters following each language's existing backend-reader convention.
The object key is always `<profile>/compute/coordination.json`. Validate profile
with the existing safe-ID grammar and backend selection/settings with
`backend_plan`. No caller can select an arbitrary write key.

Get returns exactly one of:

- `{"status":"present","etag":string,"document":object}`.
- `{"status":"absent"}` only for the CLI's structured service error code
  NoSuchKey on the GetObject operation.
- `{"status":"error"}` for every other failure, including NoSuchBucket,
  AccessDenied, generic 404s, invalid/empty JSON, missing ETag and cleanup failure.

Put accepts exactly the reducer's output shape, condition and document. Reuse
the coordination module's strict document validator rather than duplicating it.
The document's profile/provider/backend identity must match the selected opts
using the same normalized identity fields. The condition is exactly
`{"if_none_match":"*"}` or `{"if_match":nonblank string}`. Reject invalid
input before a subprocess or remote call. Return `{"status":"written","etag":...}`
only after exit zero and a valid nonblank ETag; return `{"status":"conflict"}`
for PreconditionFailed or ConditionalRequestConflict service errors on PutObject;
all other results are `{"status":"error"}`. Error does not prove a write failed:
the coordinator must read back its unique write_id before deciding what happened.
This API never retries writes and never dispatches provider work.

Each call creates a private temporary directory with mode 0700 and files with
mode 0600, cleaned up on all exits. Files include the returned GetObject body or
the PutObject document and any backend-only credential/config files. Limit a
read document or serialized write to 2 MiB. Decode body bytes as strict UTF-8;
reject byte-order marks, malformed bytes and other encodings. Reject known bound
R2 credential values, including their JSON-escaped representation, in serialized
command arguments or write intentions before execution, and in returned ETags
or documents before returning. Never return raw stdout/stderr,
exception text, credentials or temporary paths. Journal reads may contain
untrusted content; validate through the reducer before trusting it and do not
log unvalidated documents. Cancellation propagates after cleanup.

For R2, remove AWS_* and COLORS_PAR_* variables from the copied backend-only
child environment, except AWS_CA_BUNDLE which may supply a trusted CA file. Write an isolated shared credentials file with a default
profile containing only the two COLORS_PAR_R2 credentials. Reject newline-bearing
credentials, blanks and REPLACE_ME before writing INI. Point
AWS_SHARED_CREDENTIALS_FILE and AWS_CONFIG_FILE at private credentials and empty
config files. This process never runs compute operations and leaves the parent
and compute environments unchanged. For S3, preserve ambient AWS authentication
and configuration; never synthesize credentials from R2 variables. Remove
COLORS_PAR_* in both modes. Set AWS_PAGER empty, AWS_CLI_AUTO_PROMPT off and
AWS_MAX_ATTEMPTS=1. Use native exact-environment execution without a shell and a
120-second process timeout. The existing read-only process runners may be reused.

Run only these command shapes, using arguments rather than shell expansion:

```
aws s3api get-object --bucket <bucket> --key <derived-key> <private-body-path>
  --region <region> --output json --no-cli-pager
aws s3api put-object --bucket <bucket> --key <derived-key> --body <private-body-path>
  --content-type application/json --if-match <etag> --region <region>
  --output json --no-cli-pager
```

First creation replaces `--if-match <etag>` with `--if-none-match *`.
R2 adds `--endpoint-url <r2-endpoint>` and region auto. S3 uses s3-region and no
endpoint argument. No secret is an argument. Disable optional request checksum
algorithms for R2 by setting AWS_REQUEST_CHECKSUM_CALCULATION=when_required and
AWS_RESPONSE_CHECKSUM_VALIDATION=when_required in that child environment.
The configured endpoint remains subject to backend validation; loopback endpoints
are used only by offline transport probes.

AWS CLI emits service failures in the stable prefix
`An error occurred (CODE) when calling the OPERATION operation:`. Match that
prefix at the start of stderr after leading whitespace, with the exact operation
name, and only when exit is nonzero. AWS CLI 2.35.11 adds the optional literal
`aws: [ERROR]: ` prefix and botocore may insert
` (reached max retries: N)` immediately after `operation`, where N is decimal
digits. Accept these exact variants only; the native loopback probe establishes
the current format. Never classify a code merely mentioned in
a service message, stdout, or later stderr line. Unsupported CLI versions and
unknown error formats fail closed. GetObject receives a precreated private body
file; PutObject receives a private serialized document. Parse only exit-zero
JSON metadata, and require a nonblank string ETag.

Tests must cover confirmed missing key vs denied/missing bucket/transport error,
conditional conflict vs ambiguous timeout, private credentials and files, no
R2 contamination of ambient AWS, bounded cleanup and strict journal inputs.
A loopback synthetic S3 server must exercise actual CLI signing, two competing
conditional creations and stale ETag updates. That demonstrates the local
transport implementation, not the behavior of a live R2 or S3 endpoint.
