# Deployment lifecycle orchestration

`orchestrate(opts, topology, request, environment=None, dependencies=None)` runs
one deployment invocation. `request` contains provider-neutral security and
network requirements. `deployment_requests(opts, topology, request, key)` builds
the shared and node requests inside the library. No consumer selects resource
addresses, credentials, provider templates, or state keys.

Create acquires the schema-2 deployment journal, declares topology, verifies or
prepares the journal-owned SSH key, destroys obsolete nodes, converges shared
resources, then invokes the same guarded node operation through native Colors
fan-out. The join collects normalized node parameters in declared topology order.
Every started sibling settles before release, including failure paths. Unknown
ownership or ambiguous journal writes prevent further dispatch and release.

All existing states are checked before local key changes. A pure full request
validation then checks capabilities and required provider bindings before
registration preflight or key generation. Legacy monolithic keys supplied by
the package must be confirmed absent. A present legacy state requires migration.

Each attempt confirms state presence and commits its journal intent before any
OpenTofu mutation. A previously failed create requires readable state with the
selected provider before retry; absent state cannot establish that a failed
apply left no resources. Declared state with a pre-existing object is refused,
as is a ready resource whose state disappeared. Declared or destroyed records
may also have a strictly verified empty state object left by OpenTofu. Shared reads include flattened
non-sensitive outputs for registered key and network references.

Delete requires compute-prevent-destroy=false, marks the deployment deleting,
destroys nodes before shared resources, then records key cleanup, cleans only
the owned local key files, records removal and retires the generation. Successful
deletes retain journal history. Explicit recreation starts a new generation.
All operations preserve failed state for review. Generic error results contain
no provider diagnostics, credentials, or private key material.

Dependency injection replaces coordinator construction, SSH preparation/cleanup,
request assembly/rendering, state presence/read/convergence, or native workflow
execution in tests. Production defaults use library implementations. Dependencies
are trusted application code, never profile input. No test injects cloud writes.

When `s3-bucket-mode=managed`, orchestration bootstraps the owned backend before
state access and rechecks its lifecycle after acquiring the journal. It retains
the bucket on compute deletion; the application calls `finalize_backend` after
all of its other stateful stages have been destroyed. See
[managed backend ownership](managed-backend.md).
