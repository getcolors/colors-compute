# Yandex compute templates

Canonical typed JSON templates for deployment-owned network resources and one
node per state. These assets do not implement lifecycle execution or package
migration. Render using the common `contracts/rendering.md` contract: replace
whole `{{key}}` values while preserving types, leave Terraform `${...}`
expressions untouched, and reject partial placeholders.

## Resources and inputs

Shared state owns `yandex_vpc_network.network`, `yandex_vpc_subnet.network`, and
`yandex_vpc_security_group.network`. It receives `cloud_id`, `folder_id`, `zone`,
`network_name`, `network_cidr`, `prevent_destroy`, and `ingress`/`egress` maps.
Each rule has `protocol`, numeric `from_port` and `to_port`, and `cidr_blocks`.
Map keys become rule descriptions. Rules are nested in the shared security
group; they do not have independent Terraform resource addresses. The security
group is dedicated to this deployment, not the account's default group.

The example supplies restricted SSH and private cluster ingress, plus explicitly
requested outbound access. No application ports or automatic outbound allow
rule are embedded in the template. IPv4 TCP/UDP/ANY CIDR rules are the initial
contract. ICMP types, IPv6, security-group peers and predefined targets need
separate validated support; the lifecycle must refuse unsupported requirements.

Node state owns `yandex_compute_instance.node` and a reserved public IPv4
`yandex_vpc_address.node`. It consumes shared `subnet_id` and
`security_group_ids`, along with:

| Input | Type and meaning |
|---|---|
| `cloud_id`, `folder_id`, `zone` | Selected provider location and ownership |
| `node_id`, `name` | Stable identity and provider resource name |
| `platform_id` | Machine platform |
| `cores`, `memory`, `core_fraction` | Numeric CPU count, GB memory and CPU share |
| `disk_size`, `image_id` | Numeric GB boot disk size and resolved immutable image |
| `public_key` | Prepared public key content, never a local file path |
| `prevent_destroy` | Boolean, defaults true before rendering |
| `allow_stopping_for_update` | Explicit boolean authorizing update-related stops |

Every owned resource has a literal destruction guard. Changing node count does
not change existing addresses because each node has a distinct state key. Image
family resolution is library preparation work; this template accepts only an
image ID and does not ignore image changes. An example image ID is fictitious
and suitable only for build/schema checks.

## Authentication, outputs and migration

The runtime maps `COLORS_PAR_YANDEX_TOKEN` to the provider process's `YC_TOKEN`.
No token, credential argument or credential read appears in these templates.
Backend configuration is independent and belongs to the library remote-state
layer.

Managed-key and opt-out modes use the same supplied `public_key` input. Instance
metadata associates it with `ubuntu`; there is no account-level key registration,
private-key read, connection block or SSH provisioner. Only compatible Ubuntu
images are supported until another validated login contract is added. Key
content, source CIDRs, image/platform constraints and zone/subnet consistency
must be validated before real mutation.

Node params include stable `node_id`, provider, observed instance name, public
`ip`, private `vpc_ip`, and `ubuntu` user/sudoer. Metadata preserves instance ID,
FQDN, reserved address ID, zone, subnet and security-group references. The join
adds role/index and prepared identity-file metadata for downstream Ansible.

The node uses a reserved address rather than optional ephemeral NAT. Existing
ONCE deployments without reserved addresses need an explicit access/state
migration; address creation or NAT reconfiguration is not implied safe by
schema validation. Moving old resource addresses, adding a dedicated security
group, and replacing family tracking with recorded image identity also require
reviewed migration plans. No state migration is implemented here.

## Verification

```sh
python3 colors-compute/providers/yandex/check.py
python3 colors-compute/providers/yandex/check.py --tofu /absolute/path/to/tofu
```

Verified on 2026-09-09 with OpenTofu 1.12.5 and pinned
`yandex-cloud/yandex` **0.120.0**. Deterministic renders, `fmt -check`,
`init -backend=false`, and `validate` passed for shared and node examples.
Provider downloads were signature-verified; caches and generated lockfiles
remained in temporary directories. The checker excludes credential environment
variables and runs no plan, apply or destroy.

These checks establish schema/expression validity, not cloud API availability,
permissions, boot/login behavior, firewall traffic, remote-state locks or safe
migration. Provider lock distribution, semantic input validation, SSH readiness,
state lifecycle and package fan-out remain library integration work.

Official resource references:
[compute instance](https://registry.terraform.io/providers/yandex-cloud/yandex/0.120.0/docs/resources/compute_instance),
[security group](https://registry.terraform.io/providers/yandex-cloud/yandex/0.120.0/docs/resources/vpc_security_group),
[reserved address](https://registry.terraform.io/providers/yandex-cloud/yandex/0.120.0/docs/resources/vpc_address).

## Image family discovery extension

The `node-discovery` packaged stage reads `data.yandex_compute_image.ubuntu`
with a provider-owned `family` input. Its node addresses match pinned-image
mode, and its lifecycle ignores `boot_disk[0].initialize_params[0].image_id`
changes so a newly published family image does not replace an existing disk.
Explicit image-ID mode retains normal replacement planning. This preserves
ONCE's existing family/pin distinction. Schema validation passed with pinned
Yandex provider0.120.0 and OpenTofu1.12.5 for the discovery variant; no image
lookup or cloud operation was executed. The library recipe selects the variant,
so packages do not branch on image-discovery implementation.
