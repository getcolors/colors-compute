# Runtime security review: Red and Green SDKs

Reviewed local SDK source and performed synthetic, offline subprocess probes on
2026-09-09. No real credentials, remote state, or cloud resources were accessed.
The new Red backend reader intentionally uses native exact-environment execution
rather than these existing helpers. Findings below remain SDK follow-up work;
this assessment did not modify either SDK.

## Confirmed findings

1. **Red accepts negative subprocess exits as success in OpenTofu steps.**
   `red/src/runtime.ts` returns exit -1 for timeouts. `red/src/tofu.ts` tests
   `exit > 0` in init, apply/destroy and output handling. An injected runner
   returning -1 for both init and destroy caused `tofuStep` to invoke destroy
   after failed init and return `red/exit: 0`. Change all these boundaries to
   `exit !== 0`; add timeout/negative-exit regression tests before reuse.
   Green's `green.tofu/failed?` similarly tests `pos?`, although its current
   shell wrapper maps launch errors to positive 127. Any future integration of
   `green.process/run-with-timeout` (which returns -1) must fix that assumption.

2. **Environment overlays retain unsafe OpenTofu controls.**
   Red runtime and `runInherit` merge process.env with supplied env. A synthetic
   child retained TF_LOG=TRACE and TF_CLI_ARGS=-lock=false despite receiving an
   explicit env map. Setting those keys to undefined removed them in Bun, but
   omission alone did not. Green `process-builder` only adds `extra-env` to the
   inherited ProcessBuilder environment; a real Clojure child likewise printed
   the two inherited synthetic settings. Green tofu-step explicitly merges its
   supplied env with System/getenv. A protected reader must construct an exact
   child environment, strip every TF_/TOFU_/COLORS_PAR_ variable, then set only
   its automation/workspace/private data-directory controls. Preserve ambient
   cloud authentication, especially AWS credentials when the backend is R2.

3. **Raw subprocess diagnostics become workflow errors.**
   Red tofuStep copied a synthetic credential marker from runner stderr into
   `red/err` verbatim. Both SDK tofu helpers embed stdout/stderr in failures;
   output parsing also returns all output values without classification.
   For protected backend sessions return a fixed error result and never return
   command records, raw state, stderr, cache paths or bound credentials. State
   params can independently contain sensitive application outputs and must not
   be logged even after filtering known backend values.

4. **Generic backend writers do not establish private file permissions.**
   Red backendAdvice wrote backend.tf.json mode 0644 under umask 022 in a
   synthetic probe. Green write-backend! uses mkdirs/spit without explicit
   owner-only modes. Neither creates an isolated private backend cache. Do not
   pass secrets to these generic writers. Protected sessions need a fresh 0700
   directory, 0600 files, private TF_DATA_DIR and recursive cleanup. R2 partial
   credentials belong only in the private credentials file; argv carries its
   path. OpenTofu copies configuration to local cache, so deleting only the
   partial file is insufficient.

5. **Existing output readers do not establish confirmed remote absence.**
   Both SDK `outputs` functions run `tofu output -json` directly, without a
   backend initialization/state-envelope check. Missing outputs are not proof
   that no remote state exists. ONCE readState wraps selected exceptions but
   its existing comments still permit unreadable state during create. New
   compute lifecycle code must not inherit this behavior. The protected reader
   accepts only a complete version-4 state envelope and returns present/error;
   absent observation and deployment coordination remain separate work.

## Positive properties and limits

SDK subprocess calls use argument vectors, so ordinary argument values are not
shell-expanded. Their optional POSIX quote helpers do not change the environment
or diagnostic issues above. Green timeout handling attempts descendant cleanup;
Red runtime timeout kills only its direct child. Neither existing tofu-step
passes a timeout configuration. Timeout, cancellation and orphan-process
handling need explicit tests before adopting a shared mutation runner.

The new Red reader tests verify its actual permissions and cleanup, no secret
values in argv, split secret-free/credential files, unchanged AWS auth, removal
of logging and injected CLI arguments, negative exits, invalid state and blocked
credential echoes. A native fake-tofu subprocess checks exact environment
behavior without network access. These are local controls, not evidence of R2
lock correctness, remote absence or cloud readiness.

The new Red reader has since added POSIX process-group termination. Its test
starts a shell that exits while a 30-second background sleep retains output
pipes; the 100 ms deadline kills the remaining group and the runner rejects
promptly. This fixes the new reader, not the pre-existing SDK runtime. Windows
still has only direct-child termination and requires a separate process-tree
implementation before claiming equivalent cancellation behavior.
