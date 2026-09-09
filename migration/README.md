# AutoMQ state migration review

`automq_plan.py` is a read-only inventory and address-mapping tool for raw
OpenTofu state version 4. It never invokes OpenTofu, contacts a provider,
fetches state, writes state, or emits resource attributes or output values.
The caller supplies an already obtained state file and a reviewed explicit
node mapping. It does not infer broker identity from resource labels or current
cluster counts.

```sh
python3 colors-compute/migration/automq_plan.py \
  --state /secure/path/old-state.json \
  --profile example \
  --node-mapping /secure/path/node-mapping.json
python3 -m unittest discover -s colors-compute/migration -v
```

Mapping format:

```json
{
  "vultr_instance.node[0]": "0",
  "vultr_instance.node[1]": "1",
  "vultr_instance.node[2]": "2"
}
```

Exit 0 means all encountered instances have a supported, unambiguous mapping
ready for human review. Exit 2 means input validation or mapping failed.
Every result has `executable: false`; this tool does not authorize a state
transfer. Successful mapping is not evidence of matching live resources,
backend ownership, safe replacement behavior, or complete expected topology.
The caller must compare this inventory with desired and historical topology.

## Proposed canonical resource addresses

State keys follow [contract version 1](../contracts/README.md). These destination
addresses define the initial migration proposal and must be checked against the
actual library adapter templates before any transfer. Changing this table and
the planner is a versioned migration change.

| Source address | Destination address | Destination state |
|---|---|---|
| `vultr_instance.node[index]` | `vultr_instance.node` | `<profile>/compute/nodes/<mapped-id>.tfstate` |
| `vultr_vpc.cluster` | `vultr_vpc.network` | `<profile>/compute/shared.tfstate` |
| `vultr_firewall_group.cluster` | `vultr_firewall_group.network` | shared |
| `vultr_ssh_key.machine` | `vultr_ssh_key.machine` | shared |
| `vultr_firewall_rule.ssh[index]` | `vultr_firewall_rule.ingress["ssh:<index>"]` | shared |
| `vultr_firewall_rule.kafka[index]` | `vultr_firewall_rule.ingress["kafka:<index>"]` | shared |
| `vultr_firewall_rule.cluster_internal[index]` | `vultr_firewall_rule.ingress["cluster_internal:<index>"]` | shared |

The optional SSH resource is absent in opt-out mode. Rule instances use their
existing string keys; node instances require nonnegative integer count keys.
Only root-module managed resources with the unaliased Vultr provider binding
are supported. Unknown resources, module resources, data resources, provider
aliases, tainted/deposed instances and missing ownership IDs require manual
review. Duplicate source addresses, destination ownership and provider IDs
within a resource type are errors. Provider IDs are compared internally and
never printed.

The report intentionally contains resource addresses and state keys, which can
include CIDR keys from firewall rules. Treat it as operational metadata. Raw
state and mapping files are external inputs and must not be committed.

## Handoff

Implemented 2026-09-09. Seven synthetic unit tests pass, covering complete
mapping, non-mutation, no secret-attribute emission, missing mappings,
duplicate destination/source/provider ownership, unknown resources/providers,
deposed/module rejection, unsafe identifiers, malformed state and unused
mapping entries. No live state was read and no infrastructure changed.

Next: reconcile destination addresses with implemented provider templates,
add legacy deployment topology validation and backend ownership checks, then
review an operator-supplied state snapshot. State backup, locking, transfer,
rollback, and replacement-free plan verification are not implemented here.

## Preventing fresh creation during rollout

Set `compute-require-existing-state: true` in a deployment whose existing
resources still need state migration. Build remains available. Create requires
an active compatible ownership journal with a prepared key and recorded shared
and node state. An absent, uninitialized, or retired journal is refused before
any journal write, key preparation, or provider mutation. Acquisition still uses
the observed ETag, and lifecycle checks read each recorded state before compute.

This guard does not import resources or move local state to R2 or S3. Preserve
the old state and complete the transfer procedure before running create. Keep
the guard enabled after migration to prevent accidental recreation if remote
state is lost. Delete retains its existing protection and recovery behavior.
