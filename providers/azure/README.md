# Azure compute render contract

Canonical typed JSON templates split Azure machine infrastructure into one
shared configuration and a single-node configuration. Shared resources are a
resource group, VNet, subnet, NSG, explicit ingress rules and subnet/NSG
association. Each node owns a public IP, NIC and Linux VM. No application,
private key or provider credential is embedded.

## Rendering and inputs

Parse template JSON and replace strings exactly matching `{{name}}` with a
deep copy of the named JSON input, preserving its type. Missing inputs and
partial placeholders fail. Preserve OpenTofu `${...}` expressions. See
`check.py` for executable substitution and deterministic example checks.

| Input | Meaning |
|---|---|
| `subscription_id` | Non-secret Azure subscription UUID; authenticate through ambient Azure CLI session |
| `resource_group_name`, `location` | Deployment-owned resource group and region |
| `network_name`, `network_cidr` | Owned VNet name and CIDR |
| `subnet_name`, `subnet_cidr` | Owned subnet name and contained CIDR |
| `firewall_name` | NSG name |
| `ingress` | Stable-keyed map: unique numeric priority 100–4095, protocol, port, source CIDR list |
| `prevent_destroy` | Literal boolean, true by default |
| `node_id`, `name` | Stable node identity and separately resolved machine name |
| `public_ip_name`, `nic_name` | Stable node-specific resource names |
| `subnet_id` | Completed shared preparation's subnet resource ID |
| `vm_size`, `disk_size_gb` | VM size and numeric OS disk size |
| `image_reference` | publisher, offer, sku and version map |
| `public_key` | Public SSH key content; no file reads or private material |

The library must validate CIDRs, names, priority uniqueness/reserved names,
subnet and subscription identity, supported image/login combinations, required
SSH ingress, and key ownership before real operations. Template substitution
does not perform semantic validation. The node uses `ubuntu` for login and
sudoer, disables password authentication, and uses a Standard_LRS OS disk.
Alternate users, image plans and disk types require explicit extensions.

The example uses the publicly known RFC 8032 Ed25519 test-vector public key,
labelled `RFC8032-TEST-ONLY`. It is deterministic and syntactically valid for
provider validation, but must be rejected by a real-create validator. The
initial placeholder string failed Azure provider key parsing; the fixture was
corrected before schema validation was declared successful. No private key was
generated, read or committed. Runtime managed and opt-out public keys follow
the same VM input contract; Azure has no shared account key registration here.

## Ownership and policy

`<profile>/compute/shared.tfstate` owns:

- `azurerm_resource_group.deployment`
- `azurerm_virtual_network.network`
- `azurerm_subnet.network`
- `azurerm_network_security_group.network`
- `azurerm_network_security_rule.ingress[stable-rule-id]`
- `azurerm_network_security_rule.deny_inbound`
- `azurerm_subnet_network_security_group_association.network`

Shared `params` returns provider, resource_group_name, location, vpc_id,
vpc_ip_range, subnet_id and firewall_id. Complete the entire shared operation,
including rules and association, before node fan-out. A subnet ID alone does
not establish completed security preparation.

Explicit ingress rules are followed by a deny-all-inbound rule at priority
4096. This overrides Azure's default broader VNet inbound allowance; private
peer traffic therefore needs explicit permitted source CIDRs and ports. Azure
processes lower priority numbers first and retains default outbound rules.
See the [Microsoft NSG documentation](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview).
This adapter does not implement outbound restriction or platform-traffic
isolation; requests requiring them must fail capability validation.

`<profile>/compute/nodes/<node_id>.tfstate` owns `azurerm_public_ip.node`,
`azurerm_network_interface.node` and `azurerm_linux_virtual_machine.node`.
The NIC is in the prepared NSG-associated subnet. Public IP allocation is
Static/Standard; private allocation is Dynamic. Node `params` returns node_id,
provider, observed name, public ip, vpc_ip, vm_id, nic_id, subnet_id, user,
sudoer and null ssh_key_id. The join adds role/index and access path metadata.
No SSH provisioner is used; runtime readiness is an explicit later step.

The resource group is deployment-owned, not an adopted shared group. Deletion
must prove all recorded nodes and foreign-resource constraints before shared
cleanup; do not rely on resource-group deletion to erase untracked resources.
Existing ONCE addresses (`once`, `node1`, `public`) require explicit migration
to these names with no unexpected replacements. Changing NSG ownership from
NIC association to subnet association is a policy migration requiring review.

## Validation and handoff

```sh
python3 colors-compute/providers/azure/check.py
python3 colors-compute/providers/azure/check.py --tofu /absolute/path/to/tofu
```

Verified 2026-09-09: OpenTofu 1.12.5 with exact pinned azurerm 4.30.0 passed
`fmt -check`, `init -backend=false`, and `validate` for shared and node examples.
Provider signature verified with key 0C0AF313E5FD9F80. The check runner excludes
credential environment variables and uses temporary initialization directories.
No plan/apply, Azure session, account query or live state operation ran.
Release packaging still needs reviewed multi-platform provider locks.

Deterministic checks verify rendered bytes, preserved types/expressions,
placeholder failures, explicit inbound denial and shared subnet association.
Schema acceptance does not verify Azure CLI authentication, resource-provider
registration, regional SKU/image availability, network behavior, SSH readiness,
remote locking or replacement-free state migration.

Handoff: templates, deterministic examples and schema checks are complete.
Remaining work is runtime semantic/capability validation, remote-state and
ownership lifecycle integration, key readiness, package adoption and reviewed
state migration. Shared resource-group adoption, alternate network modes,
role-specific NSGs, egress control and private-only hosts are unsupported in
this milestone.
