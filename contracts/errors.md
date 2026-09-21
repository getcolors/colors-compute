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

Codes distinguish `command_failed`, `missing_credentials`, `state_unreadable`,
`state_absent`, `identity_mismatch`, `unsafe_plan`, `invalid_request`,
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
assignments are redacted; structured state/plan dumps are suppressed. Terminal
control sequences are removed. Useful toolchain errors such as asdf's missing
version message remain visible. Sanitization applies before truncation so a
truncated private-key block cannot escape filtering.

Packages own presentation and environment-specific advice. Alice can show the
executable and stderr and suggest its direnv toolchain for an asdf version
failure. The library does not prescribe direnv or tell the caller to rerun an
operation automatically.
