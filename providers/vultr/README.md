# Vultr compute render contract

These canonical OpenTofu JSON templates are the first provider adapter assets.
They contain machine, network, access-registration and provider-firewall
resources; application installation remains downstream. They are not a
lifecycle runner or a claim that package migration is complete.

## Rendering

Parse each `.tf.json.template` as JSON. Recursively replace a string that is
exactly `{{name}}` with a deep copy of the corresponding input value, preserving
its JSON type. Missing inputs and partial placeholders are errors. Do not
interpolate `${...}` strings: they belong to OpenTofu. `check.py` contains the
reference substitution and checks committed representative renders.

Write `shared.tf.json.template` as shared-state `main.tf.json`. In managed-key
mode also write `shared-keygen.tf.json.template` as `key.tf.json` in that same
configuration. In opt-out mode omit the key fragment entirely. No conditional
`count` or `for_each` is used for the key, so its address is always
`vultr_ssh_key.machine`; changing key mode is an explicit ownership migration.
The key fragment exposes `ssh_key_id` separately from shared `params`.

Write `node.tf.json.template` once per node, using its isolated configuration
and stable node state key. Node input references are resolved shared-resource
IDs, not new shared-resource definitions.

| Input | Type and meaning |
|---|---|
| `profile` | Validated deployment identity, registration name |
| `name`, `firewall_name` | Validated provider display names |
| `region`, `plan` | Vultr region and machine plan identifiers |
| `network_address`, `network_prefix`, `network_cidr` | Network address string, numeric prefix, full CIDR string; must agree |
| `ingress` | Map of stable rule IDs to `protocol`, `port`, `ip_type`, `subnet`, numeric `subnet_size` |
| `public_key` | Public key content only, prepared once by the library; fixture placeholder is build-only |
| `prevent_destroy` | Boolean; true by default, literal required by OpenTofu lifecycle |
| `node_id` | Stable topology identity |
| `os_id` | Numeric Vultr image identifier |
| `firewall_group_id`, `vpc_ids` | Shared firewall ID string and shared network ID list |
| `ssh_key_ids`, `ssh_key_id` | Node registration ID list; optional primary registration reference represented by null in opt-out mode when no single reference exists |
| `user`, `sudoer` | Validated image-specific login and privilege identities |

The library must validate region/network consistency, names, source CIDRs,
nonempty SSH rules, public-key authenticity on real create, required node
connections, rule collisions and supported image/login behavior before apply.
The reference substitution alone does not validate these semantic constraints.
`examples/inputs.json` illustrates AutoMQ's public SSH/Kafka and private
controller/internal ingress as data; the template contains no AutoMQ logic.

No provider credential appears in templates. The runtime supplies
`VULTR_API_KEY` from `COLORS_PAR_VULTR_API_KEY` only to the provider operation.
The templates do not contain backend configuration: the library's remote-state
layer supplies it independently and isolates R2 credentials from compute.

## Ownership and migration addresses

Shared state is `<profile>/compute/shared.tfstate`. It owns
`vultr_vpc.network`, `vultr_firewall_group.network`, optional
`vultr_ssh_key.machine`, and `vultr_firewall_rule.ingress[stable-rule-id]`.
Node state is `<profile>/compute/nodes/<node_id>.tfstate`, owning exactly
`vultr_instance.node`. These match [the AutoMQ migration planner](../../migration/README.md).
Existing AutoMQ firewall keys are mapped as `ssh:<CIDR>`, `kafka:<CIDR>`, and
`cluster_internal:<port>`. New integrations may supply other stable IDs.

Node `params` returns `node_id`, `provider`, observed `name`, public `ip`,
private `vpc_ip`, `user`, `sudoer`, and the shared `ssh_key_id` reference.
It returns no private key content. Role/index and any required identity-path
reference remain orchestration metadata added before the complete node join.

The node intentionally omits Vultr `hostname`, whose existing package contract
warns that changes reinstall the OS. It also omits SSH provisioners and private
key reads: the lifecycle must perform readiness checks after node creation and
before application convergence. Repeated destroy must not depend on local key
files. Existing provisioner removal and address moves still require a reviewed
replacement-free migration plan.

## Validation and limits

```sh
python3 colors-compute/providers/vultr/check.py
python3 colors-compute/providers/vultr/check.py --tofu /absolute/path/to/tofu
```

The second command initializes and validates all three rendered configurations
in temporary directories with backend initialization disabled and credential
environment variables excluded. It invokes no plan, apply or destroy.

Verified on 2026-09-09 with OpenTofu 1.12.5 and downloaded Vultr provider
2.32.0: deterministic renders, `fmt -check`, `init -backend=false`, and
`validate` passed for shared keygen, shared opt-out and node configurations.
The registry did not provide GPG keys for this provider, so OpenTofu reported
that signature validation was skipped. Locks/cache were confined to temporary
directories. The constraint currently mirrors existing packages (`~> 2.0`);
release packaging must establish the reviewed provider-lock distribution.

These checks verify provider schema and expression validity, not live API
availability, permission scopes, image boot/login behavior, firewall traffic,
remote locks, SSH readiness or migration replacement behavior. The network
resource deliberately remains `vultr_vpc`, matching AutoMQ's documented live
VPC API behavior; schema acceptance alone is not an API availability test.

## Handoff

Provider templates and deterministic representative documents are complete;
all three configurations passed actual provider-schema validation. No live
resources, credentials or state were accessed. Remaining work is the library
renderer integration, semantic validation, remote state and shared ownership
lifecycle, readiness checks, and package fan-out integration. The canonical
resource names agree with the migration planner; no planner changes were needed.
