# Managed S3 backend buckets

`bootstrap_backend(opts, environment?)` creates a deployment-owned S3 state
bucket before the first remote state read. `finalize_backend(opts, environment?)`
deletes it after the entire application has been destroyed. Blue and Red export
these asynchronous operations at the package root. Green exposes synchronous
`bootstrap-backend!` and `finalize-backend!` in
`io.github.getcolors.compute-managed-backend`. Native implementations use the AWS
CLI and its standard AWS credential chain; they do not interpret application
`COLORS_PAR_` variables as AWS credentials.

The explicit option `s3-bucket-mode` is `external` by default, preserving existing
operator-owned state buckets. `managed` requires `provider-backend=s3`, an
explicit `s3-bucket`, `s3-region`, and deployment `profile`. Managed bucket names
contain lowercase letters, digits, and hyphens; dots are refused. Build and dry
run do no I/O. External buckets are never created, changed, emptied, or deleted
by these operations.

Bootstrap checks AWS account identity and distinguishes a confirmed missing
bucket from denied or uncertain access. Creation uses owner-enforced object
ownership, profile/account/purpose bucket tags, and an owner marker at
`_colors/backend-owner.json`, written with `If-None-Match: *`. Every subsequent
invocation verifies the tags, regional location, and marker's exact
account/bucket/region/profile identity before configuring public-access blocking,
AES256 encryption, and versioning. Existing buckets without this ownership are
refused, never adopted or retagged. An interrupted creation before the ownership
marker is established requires explicit operator recovery; unknown ownership is
not inferred from the bucket name. `compute-require-existing-state=true` also
refuses creation of a missing backend. Delete never creates a missing bucket.

Compute orchestration invokes bootstrap before legacy/compute state reads and
rechecks it after taking the compute journal lease. Packages whose DNS, storage,
or other stateful tools run before compute must call bootstrap earlier. Those
packages must place their states beneath `<profile>/` in this dedicated bucket.

Compute deletion retains the backend so later package stages can destroy their
resources and update state. The package invokes finalize only after its full
delete DAG, with `compute-prevent-destroy=false`. Finalize acquires the compute
journal lease and requires a validated retired compute journal. It lists all
latest objects, allowing only the ownership marker, compute journal, and
`<profile>/*.tfstate` objects with a version-4 envelope and no resource instances.
Unexpected latest objects, active states, and lockfiles refuse deletion before
any object purge. All CLI listings use automatic pagination.

After this check, finalize conditionally changes the owner marker to `deleting`
and records `colors:phase=deleting` on the already-owned bucket. Bootstrap refuses
this durable deletion phase. Finalize deletes object versions and delete markers
in batches of at most 1000, leaving ownership marker versions until last, then
deletes the empty bucket. Deletion intentionally removes retired state history;
this is appropriate for explicitly managed disposable deployment buckets. The
compute lease is not released after its journal has been removed.

An interrupted purge can resume using the deleting owner marker. If interruption
happens after its final version was removed, the matching account/profile/purpose
tags, region, and durable deleting tag authorize the empty-bucket cleanup retry.
A missing marker without that deleting receipt is refused. A missing bucket is
an idempotent `absent` result. Failed AWS requests retain resources for review;
these operations do not print raw AWS diagnostics or credential values.

`backend_presence(opts, environment?)` (Green `backend-presence`) is the
read-only companion: it resolves the caller account and issues one
owner-scoped `head-bucket`, returning `present` or `absent` and nothing else.
External mode, build and dry-run return `skipped` without any AWS call. It never
creates, tags, or acquires the journal. The GCS and OCI managed backends expose
the same operation through their own bucket reads, and OCI still confirms
absence with the compartment listing before answering `absent`.

Deployment inspection uses this operation to distinguish a retired managed
backend from a transport error: when the journal read fails and presence
confirms the managed bucket is gone, inspection reports `absent`, so a repeated
delete routes to finalization instead of refusing. A present bucket, an
external bucket, or a failed presence check leaves the read as `error`.
