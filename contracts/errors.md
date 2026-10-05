# Runtime failures

`compute_node` / `compute-node!` returns structured failures in every color:

```json
{
  "status": "error",
  "error": {
    "code": "command_failed",
    "stage": "init",
    "message": "Required command failed.",
    "command": ["tofu", "init"],
    "executable": "/home/operator/.asdf/shims/tofu",
    "exit_code": 126,
    "stderr": "No version is set for command tofu",
    "infrastructure_changes": "none"
  }
}
```

Required fields are `code`, `stage`, `message`, and `infrastructure_changes`.
Missing-credential errors may include `credential`, the required environment
variable name, without its value. Command failures also report a safe command label, exit code, and resolved
executable when available. Sanitized stderr is optional. Full arguments are
never returned: they may contain credentials, local paths, or key material.
The executable is resolved using the supplied environment and working directory,
not the assistant's or caller's unrelated shell.

Native runner failures may also include `command_reason`: `executable_not_found`
when no command candidate exists in the supplied PATH, `process_start_failed`
when a candidate cannot run (including permissions, invalid cwd, or a missing
script interpreter), or `timeout` when execution or captured output exceeds its
deadline. These failures retain `exit_code: -1`; that status alone never proves
an executable is missing. A launched child's nonzero exit, including 127, has
no reason field. Custom runners may omit it; unsupported reason values are
not forwarded. Consumers should fall back to the safe message if the field is
absent or unfamiliar. See [execution details](node.md#command-failure-diagnostics).

Codes distinguish `command_failed`, `missing_credentials`, `state_unreadable`,
`state_inconsistent`, `state_absent`, `identity_mismatch`, `unsafe_plan`, `invalid_request`,
`key_access_failed`, `filesystem_error`, and `internal_error`. Stages identify
`validate`, `build`, `credentials`, `init`, `state`, `plan`, `plan-validation`,
`apply`, `access`, or `cleanup`. Consumers should tolerate future codes/stages
and use the safe message as a fallback, rather than describing every failure as
an ownership mismatch.

`infrastructure_changes` is `none` before apply is invoked and `possible` from
immediately before that invocation onward, including failed apply, failed output
validation, and failed key retrieval after apply. It describes this operation;
it never claims that resources from an earlier run do not exist. Diagnostic
collection does not retry commands or authorize cleanup. Cancellation continues
to propagate instead of becoming an ordinary error result.

Diagnostics never expose command stdout, raw state, plans, or arbitrary caught
exception messages. Stderr is bounded to 2,000 characters after sanitization.
Known credential values, encoded variants, private-key blocks, and sensitive
assignments are redacted; structured state/plan dumps and OpenTofu JSON source
excerpts are suppressed without discarding surrounding plain-text diagnostics.
For example, an OCI `401-NotAuthenticated` error remains visible when OpenTofu
also prints a JSON configuration excerpt. Terminal control sequences are removed.
Useful toolchain errors such as asdf's missing version message remain visible. Sanitization applies before truncation so a
truncated private-key block cannot escape filtering.

Packages own presentation and environment-specific advice. Alice can show the
executable and stderr and suggest its direnv toolchain for an asdf version
failure. The library does not prescribe direnv or tell the caller to rerun an
operation automatically.

## Google user reauthentication

Failed Google provider `tofu plan` commands whose stderr contains the OAuth
`invalid_grant` / `invalid_rapt` reauthentication response include the optional
`auth_reason: "google_reauth_required"`. Classification happens before stderr
sanitization and exposes only this authored constant; structured fragments remain
suppressed while safe surrounding diagnostic text is preserved. Generic token
revocation, permission errors, other providers, and backend commands do not
receive this hint. The error code remains
`command_failed`; existing consumers may ignore the additional field.

This is a diagnosis, not authorization to replace credentials. A caller may
interactively renew local user ADC and retry a read-only connection lookup once.
It must verify the active credential source, preserve cancellation, and never
use this hint to replay an apply or to switch service-account/federated identity.
The library neither launches login nor retries the operation.

## Inconsistent state

A valid, readable state envelope with no resources but remaining outputs returns
`state_inconsistent`, rather than `state_unreadable`. Its authored message explains
that retrying unchanged will fail and asks the operator to back up the affected
state and verify actual provider resources before recovery. It warns against
blindly deleting state or replacing the SSH identity. Callers should identify
the affected state using their known deployment context. No state output names
or values are included, and no recovery or provider mutation is attempted.
The current stage and infrastructure-change status remain accurate, including
`possible` if an apply has already been invoked.
