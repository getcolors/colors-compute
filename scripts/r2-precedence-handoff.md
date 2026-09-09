# R2 credential precedence review

Assessed 2026-09-09 against exact OpenTofu 1.12.5 source and executable. No
remote service or real credentials were used. The probe binds a loopback HTTP
server and passes fixed fake AWS/R2 values to the backend process.

## Result

Explicit R2 `access_key` and `secret_key` in private backend configuration do
not inherit ambient AWS session tokens in this version. A localhost signing
probe observed R2's configured access key and no X-Amz-Security-Token header,
even with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN and
AWS_SECURITY_TOKEN present together. Initialization succeeded against the fake
S3 endpoint's empty bucket response.

An ambient AWS_PROFILE naming a nonexistent profile does cause initialization
to fail before any HTTP request, despite the explicit static credentials.
Therefore profile/config contamination is a concrete issue, while token
merging is not observed and is contradicted by the pinned source path.

## Source chain

[OpenTofu v1.12.5 backend.go](https://github.com/opentofu/opentofu/blob/v1.12.5/internal/backend/remote-state/s3/backend.go)
constructs awsbase.Config with AccessKey, SecretKey and Token obtained directly
from backend attributes; token has no environment fallback at this step.
[Its go.mod](https://github.com/opentofu/opentofu/blob/v1.12.5/go.mod) pins
aws-sdk-go-base/v2 to v2.0.0-beta.72.

[That dependency's aws_config.go](https://github.com/hashicorp/aws-sdk-go-base/blob/v2.0.0-beta.72/aws_config.go)
selects NewStaticCredentialsProvider(c.AccessKey, c.SecretKey, c.Token) whenever
any explicit authentication field is nonempty. It subsequently loads AWS SDK
configuration with that credentials provider. Profile/config loading remains
possible even though the credentials provider is explicit.

## Recommended binding

Keep the current private-file R2 access_key/secret_key binding. Do not rewrite
global AWS access keys or session tokens. For pinned 1.12.5, stripping
AWS_SESSION_TOKEN/AWS_SECURITY_TOKEN is not necessary to prevent mixed signing.
Adding an explicit empty token is redundant on the verified code path.

For the separate backend-only R2 reader process, remove AWS_PROFILE and
AWS_DEFAULT_PROFILE from a copied child environment. Preserve the parent
process and the compute-provider process environment. Consider additionally
isolating shared AWS config/credential files using private empty files or
explicit backend shared_config_files/shared_credentials_files if reproducible
shared-config interference must be prevented. That policy requires contract
changes and dedicated tests; it must not accidentally apply to S3 sessions,
which rely on the ambient AWS chain.

AWS_SDK_LOAD_CONFIG, shared config paths, endpoint overrides, web identity and
other AWS environment settings deserve a full backend-only environment policy,
not piecemeal assumptions. The minimal demonstrated fix is profile-selector
isolation. Do not strip profile selectors from a combined AWS-compute/R2-backend
apply process: that would break compute authentication. The current read-only
session is separate, making backend-only isolation possible.

## Native reader regression probe

`backend_http_probe.py` supersedes the scratch diagnostic. It invokes the actual
Blue, Green, and Red read-state functions with their native runners and real
OpenTofu 1.12.5, against a loopback-only synthetic S3 server. It accepts portable
`--tofu`, `--bb`, and `--bun` arguments (or TOFU/BB/BUN environment settings),
resolves executables before sanitizing the environment, and defaults to all
three colors. No real credentials or cloud service are involved.

All three readers passed: each returned the synthetic existing state, made only
seven GET/HEAD requests, signed with the configured R2 access key, and omitted
session-token headers even though synthetic ambient AWS keys and both session
token variables were supplied. Invalid AWS_PROFILE and AWS_DEFAULT_PROFILE
values were also supplied; the R2-only child profile isolation let all three
reads succeed. Unexpected paths and PUT/DELETE/POST/PATCH attempts fail the
probe. The server does not log Authorization or any credential values.

```sh
python3 scripts/backend_http_probe.py --tofu /path/to/tofu --bb /path/to/bb --bun /path/to/bun
```

The probe proves private partial-backend JSON acceptance and auth precedence
through the actual library readers. It does not prove real R2 API compatibility,
remote locking, cloud authorization, coordination, or any mutation lifecycle.
The preceding source review's broader AWS shared-config concerns remain open.

Handoff: R2-only child profile isolation is now implemented and the all-color
native-reader probe passes. The parent process and S3 profile selection remain
unchanged. Green unit tests pass 22 tests / 161 assertions, including mathematical
JSON integer parity and S3 profile preservation. No commits or live operations
were performed by this subagent.
