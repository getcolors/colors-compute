# Managed GCS backend buckets

Select `provider-backend=gcs` with `google-project`, `gcs-bucket`, and
`gcs-region`. External buckets are the default. Set `gcs-bucket-mode=managed`
to let the deployment create and retire its bucket. Build and dry-run events
skip bucket operations.

## Authentication and ownership

The transport uses `gcloud auth print-access-token --quiet` and sends the token
to the Cloud Storage JSON API. Managed operations also call Resource Manager
`GET /v1/projects/{google-project}` with that token. The active account needs
`resourcemanager.projects.get` on the configured project, along with the
Storage permissions required to read bucket metadata, manage bucket protection,
and read, write, list, and delete object generations. Bucket creation and
finalization also require bucket create and delete permissions. Resource Manager
authorization failures stop the operation before bucket access.

Every managed bootstrap or finalization verifies the returned `projectId` and
requires `projectNumber` as a positive decimal string. The bucket's
`projectNumber` must equal that resolved number. This check happens before any
ownership marker write, protection update, or deletion. Creation requests name
the configured project, and the returned bucket must pass the same check before
the marker is written. Missing or malformed project numbers fail verification.

The bucket must also have the configured location and these labels:

- `colors_project` equals `google-project`.
- `colors_profile` equals `profile`.
- `colors_purpose` equals `managed-backend`.

The `_colors/backend-owner.json` marker must contain schema 1 and an identity
with exactly the configured project, bucket, region, and profile. Labels and
marker contents do not prove which project owns the bucket. The API's
`projectNumber` supplies that check. See Google's definitions of
[project lookup](https://docs.cloud.google.com/resource-manager/reference/rest/v1/projects/get)
and [bucket metadata](https://docs.cloud.google.com/storage/docs/json_api/v1/buckets).

## State reads and protection

OpenTofu stores the default workspace state at
`{logical-state-key}/default.tfstate`. Coordination writes use object generation
preconditions. Readers fetch a specific generation after reading its metadata.
An object GET returning 404 means the object is absent only if a separate bucket
GET succeeds with the expected bucket name. A missing bucket, denied bucket
read, or malformed metadata fails the read. This rule also applies to external
buckets, whose readers therefore need `storage.buckets.get` permission.
A direct bucket GET returning 404 still permits managed creation or an absent
result during deletion.

Bootstrap enables uniform bucket-level access, enforces public access
prevention, enables versioning, and disables soft delete. Existing owned buckets
receive the same settings under a metageneration precondition.

## Finalization

Finalization requires `compute-prevent-destroy=false` and a retired compute
journal under the lifecycle lock. It refuses unexpected live objects and state
files with resource instances. After the marker enters `deleting`, the bucket
receives a matching phase label. Cleanup lists every object generation and
deletes each under its generation precondition, with marker generations last,
then deletes the bucket.

A retry can finish deletion when the marker is gone and the bucket retains its
`deleting` phase label. Project number, location, and ownership labels still
must match. No live cloud test is implied by the local transport and lifecycle
regression suite.
