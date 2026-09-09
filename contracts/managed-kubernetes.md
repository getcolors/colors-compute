# Managed Kubernetes capability

This capability owns a provider-managed control plane and one homogeneous worker
pool. It is separate from VM topology: no SSH key lifecycle, synthetic VM node,
fan-out or Ansible node collection is used. Initially Vultr Kubernetes Engine
and DigitalOcean Kubernetes have reviewed adapters. Other selections refuse.

The library takes `profile`, `provider-compute`, existing R2/S3 backend settings,
and a request containing only optional `legacy_state_keys`. Library adapters
resolve historical provider option names into the neutral template inputs
`name,region,version,size,count,prevent_destroy`. The request count is a
positive integer, maximum 1000. `compute-prevent-destroy` retains its boolean
semantics. DigitalOcean keeps `ha=false` and reads cluster/service CIDRs from
the resource; it does not silently select VPC-native networking. Registry,
application manifests, admission policies and application acceptance remain
package responsibilities.

`plan_managed_kubernetes` produces deterministic typed JSON documents without
credentials or kubeconfig. `managed_kubernetes` performs create/delete through
the journal and protected executor. Normalized public params contain `provider`,
`kind:"managed-kubernetes"`, `name`, `cluster_id`, `endpoint`, and optional
IPv4 `pod_cidr`/`service_cidr`. Sensitive `kubeconfig_b64` is permitted only through a
reviewed output descriptor and a private file sink. It never appears in returned
params, stdout, an exception, argv, a build artifact, or generated template input.
The return value may name the kubeconfig path. State and temporary directories
retain the existing secret handling requirements.

`managed_application_settings` returns provider-selected load-balancer annotations,
the storage-class override, and the pod CIDR used by application manifests.
Vultr retains its configured pod range; DigitalOcean uses the observed range.
These mappings belong in the library's managed provider descriptor.

`managed_application_artifacts` supplies the selected provider's cleanup and
ingress verification scripts. Applications can require named artifacts; an
unsupported requirement fails before rendering. Applications own Kubernetes
workload deletion and retain the original volume IDs and load-balancer address
across retries. The library checks those resources at the provider and refuses
unverified cleanup. Credentials reach curl through standard input.

## Ownership

The journal uses the same `<profile>/compute/coordination.json` key as VM
coordination, so switching between VM and managed compute cannot acquire a different lock.
Schema 3 has exactly:

```
{schema_version:3,kind:"managed-kubernetes",identity,revision,write_id,
 lock:{state,run_id},generation,status,
 shared:{phase,operation,operation_id}}
```

Identity remains the exact profile/provider/backend tuple. Status is `active` or
`retired`; generation is a safe positive integer. Phases are `declared`,
`running`, `ready`, `failed`, `destroyed`. Declared has null operation/id;
otherwise operation is `create` or `destroy` and id is safe. Ready implies create,
destroyed implies destroy, and running requires the held lock. Retired requires
destroyed. No extra fields, SSH metadata, nodes or arbitrary user content exist.
VM reducers refuse schema 3; managed reducers refuse schemas 1/2. The immutable
kind and journal-key collision prevent VM/managed provider switching.

Events have the existing exact envelope `type,run_id,write_id,target_etag`, with
`managed/` prefix. Acquire/release retain conditional-write semantics, fresh
write IDs, no timed takeover, and no action before confirmed CAS. Shared start,
destroy, complete and fail use exactly `operation_id`; retry uses exactly
`evidence:"readable-state"`; recreate has no fields. Starts require active
status, phase declared/ready (create) or declared/ready/failed (destroy), and a
fresh operation ID. Failed create requires readable-state retry to declared;
release refuses running operations. Recreate requires retired/destroyed and
increments generation, restoring declared. Successful destroy retires the
journal. Failed/uncertain operations preserve ownership and refuse blind resume.

Managed state uses `<profile>/compute/managed-kubernetes.tfstate`. Native create
requires legacy combined infrastructure keys to be absent before registry or
compute work. Existing package states combining cluster and registry require an
explicit reviewed state split; normal execution never guesses resource ownership.
The package registry stage uses its own remote state after the split. Destroy
withdraws application workloads and load balancers, destroys its owned registry
resources according to existing adopt/create semantics, then destroys compute.

Provider schemas confirm VKE `kube_config` is base64 and DOKS
`kube_config[0].raw_config` is raw text; the adapter normalizes encoding. See
[Vultr resource](https://registry.terraform.io/providers/vultr/vultr/latest/docs/resources/kubernetes)
and [DigitalOcean resource](https://registry.terraform.io/providers/digitalocean/digitalocean/latest/docs/resources/kubernetes_cluster).

Provider template schemas were validated with OpenTofu 1.12.5 using init
`-backend=false`, fmt and validate: Vultr 2.32.0 and DigitalOcean 2.51.0.
These checks do not establish remote create or destroy success.
