# Render and backend planning contract

These pure functions prepare data. They do not initialize remote state, acquire
locks, or execute cloud operations. All three colors must implement them.

`render_template(value, inputs)` recursively walks a JSON document. A string
exactly matching `{{key}}`, where key matches `[a-z_]+`, is replaced by the
input value with its JSON type preserved. Lists and maps recurse. Other scalar
values remain unchanged. Terraform `${...}` expressions are not templates.
Missing input throws `missing template input: <key>`. Partial or malformed
placeholder strings containing `{{` or `}}` throw `template placeholders must
occupy the entire string`. Map keys are not interpolated. Input maps must not
be mutated. This function is for library-owned templates, never untrusted code.

`backend_plan(opts, state_key)` reads `provider-backend`, profile-independent
backend configuration, and returns:

```text
{config: {terraform: {backend: {s3: <settings>}}},
 credential_bindings: {<COLORS_PAR variable>: <backend option>},
 environment: {}}
```

S3 settings are bucket from `s3-bucket`, region from `s3-region`, key from
state_key, and `use_lockfile: true`. Bindings are empty. Ambient AWS credentials
remain untouched. R2 settings are bucket from `r2-bucket`, region `auto`, key,
`endpoints: {s3: <r2-endpoint>}`, `use_lockfile: true`, `use_path_style: false`,
`skip_credentials_validation: true`, `skip_metadata_api_check: true`,
`skip_region_validation: true`, `skip_requesting_account_id: true`, and
`skip_s3_checksum: true`. R2 bindings map COLORS_PAR_R2_ACCESS_KEY_ID to
access_key and COLORS_PAR_R2_SECRET_ACCESS_KEY to secret_key. They MUST NOT map
to AWS environment variables. No credential values enter this result.

Unknown backend throws `:provider-backend must be one of r2, s3`. Required
backend keys are validated sorted, with first missing key throwing
`:<key> is required`. Blank and REPLACE_ME values count as missing. State key
must be a nonempty string whose slash-separated components match
`[a-zA-Z0-9][a-zA-Z0-9_.-]*`, with no `.` or `..` component; else throw
`invalid state key`. Validate selection, required keys, then state key.

Native lockfile configuration does not prove R2 locking or deployment-wide
coordination. Both require execution evidence before production migration.
The runtime layer must bind R2 credentials through protected partial backend
configuration and protect OpenTofu's cached configuration. It must not mutate
the ambient AWS credential chain. Actual credential binding is not implemented
by this planning operation.

Configuration fields follow the [OpenTofu S3 backend documentation](https://opentofu.org/docs/language/settings/backends/s3/).
Inline backend credentials persist in OpenTofu's local cache. This is why
runtime credential binding needs explicit file protection and redaction.
