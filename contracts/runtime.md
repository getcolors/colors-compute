# Node runtime

The [single-unit contract](node.md) replaces temporary read-only sessions and the
journal-authorized executor. Every OpenTofu command runs in the persistent SDK
unit directory with the default `.terraform` cache. There is no independent
caller-selected temporary backend context.

The native runner uses argument vectors and an exact environment. Logging and
injected CLI arguments are removed; backend object credentials remain separate
from compute provider credentials. Failures expose [structured diagnostics](errors.md) with sanitized, bounded
stderr rather than raw state, plans, key material, or command stdout. Nonzero exits fail. Cancellation
terminates and waits for owned local processes, and does not authorize later steps.

Build is credential-free. Runtime state validation checks provider and unit
identity before mutation, and read failure never authorizes absent-state behavior.
The caller serializes filesystem operations for the same unit directory; native
OpenTofu locks protect infrastructure state operations.
