# Managed OCI backend buckets

`provider-backend: oci` stores OpenTofu state in OCI Object Storage through its
S3 compatibility API. OpenTofu 1.11.5 has no native `oci` backend. The generated
S3 backend uses the namespace's regional compatibility endpoint, path-style
requests and a conditional lockfile. It creates no AWS resources.

Set `oci-bucket`, `oci-region`, `oci-namespace` and `oci-bucket-mode: managed`.
Bootstrap also requires `oci-compartment-id` and the deployment `profile`.
The native OCI CLI uses `oci-config-file-profile`, default `DEFAULT`, and
`oci-auth`, default `SecurityToken`, from the operator's `~/.oci/config`.
`COLORS_PAR_OCI_ACCESS_KEY_ID` and `COLORS_PAR_OCI_SECRET_ACCESS_KEY` supply the
operator's OCI customer secret credentials for the OpenTofu backend. These are
bootstrap credentials, separate from application credentials. Keep them out of
`colors.yml`, generated documents and published deployment evidence.

Bootstrap creates the bucket before reading remote state, with versioning and
`NoPublicAccess`. It records the profile and purpose in freeform tags and writes
`_colors/backend-owner.json` with native `If-None-Match: *`. That marker binds the
bucket to its namespace, region, compartment and profile. Existing unowned
buckets are refused. OCI can return the same 404 for missing and denied buckets,
so bootstrap must confirm absence by listing the entire authorized compartment
before creating a bucket or reporting an idempotent absent delete.

The compute journal uses the native OCI API and `If-Match` for conditional
updates. Live OCI S3 compatibility testing on 2026-09-11 returned 412 for a
competing `If-None-Match` create but accepted a stale `If-Match` replacement.
The native API rejected the same stale replacement with 412. Using S3-compatible
journal writes would permit two operators to overwrite each other's leases.

Finalize requires `compute-prevent-destroy: false` and a retired compute journal.
It acquires the journal lease and checks every current object before purging.
Only the ownership marker, the compute journal and empty version-4 state files
under `<profile>/` are permitted. Live resources, lockfiles and unexpected
objects refuse deletion. A native conditional marker update records `deleting`,
then a durable bucket tag permits interrupted cleanup to resume. Bootstrap
refuses this deletion phase. Finalize paginates all versions and delete markers,
deletes marker versions last, then deletes the empty bucket. Application stages
must finish their deletion before invoking finalize.

## Recovering a failed initial node create

A provider rejection can leave an empty node state with `serial: 1` and no
`params` output. Ordinary convergence refuses a failed node without readable
provider ownership. Changing the shape alone does not authorize another create.

Use the explicit recovery function after reviewing the failed operation. Green
exports `recover-absent-oci-nodes!` from
`io.github.getcolors.compute-recovery`. Red and Blue export
`recover_absent_oci_nodes`. Pass the deployment options and a map from node ID
to the current failed `operation_id`. Read these IDs from the native journal
again immediately before recovery. IDs from a previous attempt are refused.
For example, after loading `opts` from the deployment's `colors.yml`:

```clojure
(require '[io.github.getcolors.compute-recovery :as recovery])
(recovery/recover-absent-oci-nodes!
 opts {"0" "current-failed-operation-id"})
```

The helper acquires the native journal lease, verifies every requested node is
a failed create with that exact operation ID, and verifies its state is absent
or an empty valid version-4 envelope. Complete native instance and boot-volume
listings must contain no nonterminated resources with the deployment prefix.
A terminating resource still blocks recovery. Only then does the helper record
`verified-provider-absence` retry events. It releases the journal lease even
when a check fails. Run the normal create command after recovery. Do not edit
the remote journal or replace its operation IDs by hand.

The recovery audit matches the deployment profile anywhere in resource display
names, including OCI's `Boot volume of instance <name>` default. It checks all
instances in the compartment and boot volumes in the scalar availability domain and every domain in
`oci-availability-domains`. It does not prove ownership of manually renamed resources or resources
moved outside that scope. Review an independent resource inventory before using
this helper after out-of-band changes. Do not change the compartment or
availability domain before recovering the original failed operation.

OCI CLI `boot-volume list` returned success with empty stdout in an availability
domain that had no boot volumes. An empty process response is not evidence of
resource absence. Recovery therefore reads the native IAAS APIs, requires JSON
arrays, and follows every `opc-next-page` header before accepting the audit.
