# OCI compute render contract

These declarative library templates provide shared network-security-group
ownership and one OCI instance per node state. The shared library executes
them under its deployment journal and managed backend lifecycle. The provider is pinned to
`oracle/oci` 8.4.0 and selects the operator's ambient `~/.oci/config` profile
through `config_file_profile`. No credential values or private-key paths occur
in the templates.

## Configuration and ownership

Render `shared.tf.json.template` as shared-state `main.tf.json`. It discovers
the configured existing subnet and its VCN with `data.oci_core_subnet.existing`.
It creates `oci_core_network_security_group.network` and stable-keyed
`oci_core_network_security_group_security_rule.rules` resources. It neither
creates nor owns the discovered subnet or VCN. Shared outputs return the subnet,
VCN, and NSG references. Destroy removes only the owned rules and NSG after all
dependent nodes are gone. Private-source ingress uses the discovered subnet's
actual CIDR. The request contains the literal `private`; the OCI template alone
resolves it to `data.oci_core_subnet.existing.cidr_block`. Existing subnet
security lists also apply, so deployments must audit their inherited rules.

Render `node.tf.json.template` once per node. It owns exactly
`oci_core_instance.node`, with a flexible shape, configured boot disk, public
IPv4 address, and VNIC attached to the supplied shared NSG IDs. Node outputs
preserve `node_id`, `provider`, observed `name`, public `ip`, private `vpc_ip`,
instance ID, `user`, `sudoer`, and `uid`; metadata includes image and availability
domain. Ubuntu examples use user/sudoer `ubuntu` and UID `1001`, preserving the
existing ONCE defaults. Those are configurable image-specific inputs, not
claims about every possible image.

There is no provider-side SSH registration in OCI. Both managed-key and opt-out
modes pass validated public-key content as `public_key`. The library prepares
or reads that content once; templates do not read any local key file. Ownership
and cleanup of the deployment key remain outside individual node operations.
There are no SSH provisioners or application-install commands.

Node source selection uses exactly one additional `image.tf.json` fragment:

- `node-image-pinned.tf.json.template` sets `local.image_id` from the supplied
  immutable image OCID and makes no image lookup.
- `node-image-discovery.tf.json.template` queries the newest Canonical Ubuntu
  24.04 image compatible with the actual configured shape, matching the previous
  ONCE lookup behavior. Empty image results fail rather than fabricate a source.

Discovery is a moving target. The lifecycle must record and pin the resolved
image for retries and later runs; changing the upstream image must not silently
replace deployed nodes. Discovery remains an explicit initial-create option.

## Typed inputs

All replacements follow [rendering.md](../../contracts/rendering.md): only
whole-value placeholders are replaced, with JSON types preserved. Terraform
expressions are left intact. Example IDs are schema fixtures, not live defaults.

| Input | Meaning |
|---|---|
| `config_file_profile` | Name selected from the ambient OCI config |
| `compartment_id`, `subnet_id` | Target compartment and discovered existing subnet OCIDs |
| `firewall_name` | Validated NSG display name |
| `rules` | Stable-ID map with `direction`, `protocol`, `cidr`, `from_port`, `to_port` |
| `node_id`, `name` | Stable node identity and validated display name |
| `availability_domain`, `shape` | Compatible availability domain and flexible shape |
| `ocpus`, `memory_in_gbs` | Numeric flexible-shape capacity |
| `nsg_ids` | Shared NSG ID list, attached to the primary VNIC |
| `boot_volume_size_in_gbs`, `boot_volume_vpus_per_gb` | Numeric boot capacity and performance |
| `public_key` | Validated public-key content; never private material |
| `image_id` | Immutable image OCID for the pinned fragment |
| `user`, `sudoer`, `uid` | Validated image-specific connection and privilege metadata |
| `prevent_destroy` | Boolean node lifecycle protection, true by default |

Rules currently support stateful IPv4 CIDR ingress/egress. `direction` is
`INGRESS` or `EGRESS`; `protocol` is OCI's `6` (TCP), `17` (UDP), or `all`.
TCP/UDP use numeric destination-port bounds; `all` uses null port values.
Dynamic blocks select the protocol's options, without duplicating node
resources. Generic application declarations need a library-owned translation
to this provider representation. No package-specific ports are hardcoded.

## Network capabilities and limits

This adapter supports discovered existing public subnets, with additional owned
NSG permissions. It does not create VCNs, routes, internet gateways, subnet
security lists, IPv6 networks, or private-only SSH routes. The existing subnet
must permit public addressing and have working internet routing. These facts
and compartment/region compatibility need runtime validation.

NSGs cannot restrict broader permissions already granted by subnet security
lists: OCI combines their allowed rules. See [Oracle's security rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm).
The runtime must inspect or otherwise establish the existing trust boundary and
refuse a requested strict-isolation capability it cannot guarantee. An NSG
schema check is not proof of deployment isolation. Guest firewall policies can
also affect connectivity and remain an explicit readiness/application concern.

The NSG attachment and protocol-specific options follow Oracle's
[instance](https://docs.oracle.com/en-us/iaas/tools/terraform-provider-oci/latest/docs/r/core_instance.html)
and [NSG rule](https://docs.oracle.com/en-us/iaas/tools/terraform-provider-oci/latest/docs/r/core_network_security_group_security_rule.html)
contracts. These schema-level capabilities do not establish live account or
image compatibility.

## Validation and migration

```sh
python3 colors-compute/providers/oci/check.py
python3 colors-compute/providers/oci/check.py --tofu /absolute/path/to/tofu
```

All representative configurations (shared, node pinned-image, node discovery)
passed deterministic render checks, `fmt -check`,
`init -backend=false -input=false -no-color`, and `validate -no-color` on
2026-09-09 using OpenTofu 1.12.5 and OCI provider 8.4.0. Provider signature was
verified with key `1533A49284137CEB`. Checks used temporary directories and an
isolated HOME without OCI config or credential environment. They did not query
OCI, read remote state, or run plan/apply/destroy. Generated locks and caches
were temporary; release lock distribution remains separate work.

Legacy `oci_core_instance.ampere_vm` needs an explicit state-address mapping to
`oci_core_instance.node`; existing images, shape, names, keys, and VNIC settings
must be preserved during migration. NSGs are newly owned resources and require
reviewed attachment/rule plans. Removing the old SSH provisioner does not prove
readiness, and renaming image data addresses does not permit image replacement.
No package can claim migration or live provider support from these assets alone.

`oci-memory-in-gbs` is optional. Omit it to let the OCI API select the shape's
memory default. Explicit values remain unchanged in the rendered request.
A missing value renders Terraform `null`, so the provider omits that API field.
This matters when the shape API reports a per-OCPU memory range of zero and
rejects explicit memory ratios. Verify the resulting instance shape after
creation rather than assuming the omitted value implies a particular size.
