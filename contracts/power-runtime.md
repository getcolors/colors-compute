# Power operations on an owned singleton

`power_deployment(opts, action, environment?, dependencies?)` accepts `start`
or `stop`. It does not apply OpenTofu or change desired power state. Initially
OCI and Vultr are supported through a packaged power descriptor. Other providers
and invalid actions fail before journal writes or provider calls.

The API requires an existing valid schema-2 lifecycle journal with matching
backend/profile/provider identity, active deployment, prepared key, ready shared
state, and exactly one desired ready node. It acquires the existing journal's
CAS lease and repeats those checks against the confirmed snapshot. It reads and
validates the node's remote params while holding the lease. Only the immutable
`provider_id` from those params may address the provider API. Neither names nor
caller-supplied IDs are fallbacks. Key mode must match recorded ownership.

The provider transport reads current state before requesting a transition. An
already settled target is idempotent. OCI stop uses SOFTSTOP. Requests have fixed
API origins and bounded response sizes/timeouts. The configured OCI profile is
passed explicitly; no credential file contents are inspected by this library.
Vultr tokens travel only in the authorization header. No raw process or HTTP
errors, response bodies, or credentials leave this API.

Success requires observing the terminal state. Start also returns a validated
current public address; callers must refresh their SSH alias from this result.
The successful result is `{status:"ready", action, cluster, key}`. Stop retains
the last recorded connection metadata; it does not claim that the stopped host
is reachable. Compute state is not rewritten by power operations.

A failure before dispatch may release the lease. Once a mutating request might
have been sent, any timeout, cancellation, malformed response, or unresolved
terminal state retains the lease for explicit recovery. A confirmed success
releases the lease before returning. Failed release returns only `{status:"error"}`.
The library never retries a mutating power request.

Build and dry-run return `{status:"planned",action}` after capability/action
validation and perform no remote reads or writes. Tests inject journal/state
readers and provider transports; passing those tests does not prove live API
credentials, permissions, or provider availability.

The OCI argument surface follows the [InstanceAction CLI reference](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/instance/action.html).
Vultr actions use the existing single-instance `start`/`halt` API. Tests must
verify one mutation at most; terminal-state polling consists only of GET requests.
