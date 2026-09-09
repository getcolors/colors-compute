# Google compute templates

These templates implement shared network resources and one Google Compute
Engine node. They prepare infrastructure configuration; they do not provide a
remote-state lifecycle, SSH readiness checks, or package migration.

## Rendering and ownership

Render JSON values according to `contracts/rendering.md`. Whole-value
`{{name}}` placeholders retain the input's JSON type; `${...}` expressions are
left for OpenTofu. `examples/inputs.json` is a build-only example. The public
key placeholder is deliberately not usable for real SSH access.

Render `shared.tf.json.template` once into the deployment shared state. It owns
`google_compute_network.network`, `google_compute_subnetwork.network`, and
`google_compute_firewall.ingress[rule-id]`. Firewall targets use one shared
network tag, which each node receives. Each ingress declaration supplies a
stable rule ID, a provider-valid unique `name`, `protocol`, `ports` list, and
`source_ranges` list. The example restricts SSH to a documentation address and
cluster ports to the private subnet. No application port is hard-coded in the
template. Egress restrictions and firewall deny rules are not implemented;
callers requiring them must be rejected before provisioning.

Render `node.tf.json.template` separately for every stable `node_id`. Each node
state owns exactly `google_compute_instance.node` and its reserved public IPv4
address `google_compute_address.node`. It consumes shared `subnetwork_id` and
`network_tag` references instead of creating shared resources. Network,
subnetwork, instance and address destruction require an explicit
`prevent_destroy: false` render. Firewall changes follow their containing shared
state's ownership; they do not acquire a separate deletion guard.

| Input | Meaning |
|---|---|
| `project`, `region`, `zone` | Google project and compatible compute location |
| `network_name`, `network_tag` | Validated shared network name and firewall target tag |
| `network_cidr` | Private IPv4 subnet CIDR |
| `ingress` | Map of stable rule IDs to name, protocol, ports, source_ranges |
| `prevent_destroy` | Boolean, default true at library configuration boundary |
| `node_id`, `name` | Stable node identity and valid Google instance/address name |
| `machine_type`, `image`, `boot_disk_size` | Machine type, resolved image reference, numeric disk size in GB |
| `public_key` | Public SSH key content prepared before fan-out |
| `subnetwork_id` | Shared subnetwork reference, within the selected region |

The runtime must validate names, source ranges, nonempty SSH access, region/zone
compatibility, public-key authenticity, and image compatibility before mutation.
This template accepts a resolved `image`; any image-project/family lookup and
precedence belongs to library preparation, not a package template. It has no
image lookup data source.

## Authentication and SSH

The Google provider is configured without credential arguments and uses ambient
Application Default Credentials. These assets neither read credential files nor
change authentication environment variables. R2/S3 backend configuration is
supplied independently by the library state layer.

The node uses the same public-key input in generated-key and opt-out modes.
There is no provider-side SSH key registration, `file()` expression, private-key
input, or SSH provisioner. The metadata configures the supplied key for `ubuntu`,
blocks inherited project SSH keys, and explicitly disables OS Login for this
metadata-key workflow. Organizations requiring OS Login are unsupported by
this adapter and require a separate supported access contract; overriding their
policy is not implied. Existing deployment changes to project-key inheritance
or OS Login require an explicit access migration review.

Node `params` reports node/provider identity, observed instance name, public
`ip`, private `vpc_ip`, and `ubuntu` user/sudoer. Metadata retains instance ID,
self-link, zone, subnetwork ID, and address ID. No UID is guessed. The join adds
topology role/index and any prepared identity-file reference before Ansible.

## Verification and limits

```sh
python3 colors-compute/providers/google/check.py
python3 colors-compute/providers/google/check.py --tofu /absolute/path/to/tofu
```

Verified on 2026-09-09 with OpenTofu 1.12.5 and pinned
`hashicorp/google` **6.0.0**: deterministic renders, `fmt -check`,
`init -backend=false`, and `validate` passed for both shared and node examples.
The provider download was signature-verified. Provider cache and lockfiles were
created only in temporary directories. The checker excludes credential
environment variables and invokes no plan, apply or destroy.

Schema validation does not establish live IAM permissions, enabled Compute API,
image availability, organization-policy compatibility, actual firewall traffic,
SSH login, backend locking, or replacement-free state migration. Shared network
changes and moving old package resource addresses require a reviewed migration.
Family image references are illustrative; a lifecycle should resolve and retain
an immutable image ID when reproducibility is required. Release packaging still
needs reviewed provider lockfile distribution.

Resource structure follows the official provider documentation:
[compute instance](https://registry.terraform.io/providers/hashicorp/google/6.0.0/docs/resources/compute_instance),
[compute firewall](https://registry.terraform.io/providers/hashicorp/google/6.0.0/docs/resources/compute_firewall),
[compute subnetwork](https://registry.terraform.io/providers/hashicorp/google/6.0.0/docs/resources/compute_subnetwork).
