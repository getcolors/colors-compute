# Read-only backend sessions

This layer reads an existing S3 or R2 state through OpenTofu. It does not create
infrastructure, initialize missing state, or authorize mutations. An unsuccessful
read never proves absence. Confirmed absence requires a separate backend object
observation and deployment coordination, which remain unimplemented.

Each color exposes `read_state(opts, state_key, environment, runner)` using its
language's naming conventions. Environment defaults to the process environment.
Runner is optional and injectable for tests. It receives a command vector, working
directory, exact environment, and a 120-second timeout. Red groups the last three
values in an options object; Green and Blue pass positional arguments. The default runner uses
native subprocess execution without a shell. This is deliberate because the
current Blue SDK merges environments and cannot remove inherited variables.

1. Render `backend_plan` from nonsecret options. Resolve its credential bindings
   only from the supplied environment. Missing, blank, or REPLACE_ME credentials
   fail before a subprocess starts.
2. Create a fresh temporary directory with mode 0700. Write `backend.tf.json`
   and `credentials.tfbackend.json` with mode 0600. The first contains the
   secret-free backend plan. The second contains only the bound backend options,
   or an empty object for S3. No credential value appears in command arguments.
3. Remove all `TF_`, `TOFU_`, and `COLORS_PAR_` environment variables. Preserve
   ambient authentication such as AWS credentials, Azure CLI configuration,
   Google ADC, and OCI configuration. Set `TF_IN_AUTOMATION=1`, `TF_INPUT=0`,
   `TF_WORKSPACE=default`, and `TF_DATA_DIR` to the private directory's
   `.terraform` child. R2 credentials never replace AWS credentials. For the separate R2 backend
   process only, remove `AWS_PROFILE` and `AWS_DEFAULT_PROFILE`: OpenTofu
   1.12.5 loads those profiles even with explicit R2 credentials, and an invalid
   ambient profile otherwise blocks the read. S3 and the parent process retain
   their selectors. This rule must not be reused for AWS compute apply processes.
4. Run `tofu init -input=false -no-color -reconfigure
   -backend-config=<absolute private credentials path>` in the private directory.
   Only after exit zero, run `tofu state pull` there. These are the only commands.
5. Accept a state JSON object only when version is integer 4, serial is an integer
   from 0 through 9007199254740991, lineage is a nonblank string, outputs is an
   object, and resources is an array. Integer refers to numeric value, so JSON
   `4.0` equals `4`; booleans and fractional serials are invalid. If `outputs.params` exists it must be an
   object whose `value` is an object. Missing params becomes an empty object for
   the existing legacy-state guard. Return `{"status":"present","params":...}`.
   Reject params if their JSON serialization contains any bound backend credential
   value. Params may still contain sensitive outputs, so callers must not log the result.
6. Any ordinary execution, configuration, parse, or cleanup failure returns only
   `{"status":"error"}`. Never return subprocess output, raw state, credentials,
   or exception text. Nonzero exit, empty output, malformed state, and absent
   params do not return an absent status. Remove the entire private directory,
   including the backend credential cache, on every normal or exceptional exit.

Cancellation propagates after cleanup. Process termination and machine failure
can leave private temporary files; no implementation promises secure erasure or
crash cleanup. Files inherit the directory's privacy even when OpenTofu creates
them. This API does not expose a persistent working directory or state writer.

Tests must inspect actual file permissions and credential placement, verify AWS
credentials survive R2 sessions, remove injected CLI arguments and logging
settings, reject failed or malformed reads, and prove cleanup on failure. Cloud
access is unnecessary for these tests. Backend absence, ownership and multi-state
serialization need separate integration tests before any create/delete API ships.
