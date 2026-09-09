# hcloud compute render contract

Canonical shared and single-node JSON templates for Hetzner Cloud machine
clusters. Shared preparation owns a network, cloud subnet, public firewall,
and optional managed SSH registration. Each node owns one server and its
private network attachment. Application configuration remains downstream.

## Typed rendering

Parse the template JSON and replace exact `{{name}}` string values with deep
copies of the named JSON input. Preserve booleans, numbers, lists and maps.
Missing inputs or partial placeholders fail. `${...}` expressions remain
unchanged for OpenTofu. `check.py` verifies this behavior and committed example
bytes. The shared key fragment is included only in keygen mode and has no
count, preserving `hcloud_ssh_key.machine` as a stable ownership address.

| Input | Meaning |
|---|---|
| `profile` | Validated deployment identity and key registration name |
| `network_name`, `network_cidr` | Owned network name and CIDR |
| `subnet_cidr`, `network_zone` | Cloud subnet contained within network and compatible network zone |
| `firewall_name`, `public_rules` | Public firewall name and list of provider rules: direction, protocol, optional port, source_ips or destination_ips |
| `public_key` | Managed public key only; real creates must reject fixture placeholders |
| `prevent_destroy` | Literal boolean, true by default |
| `node_id`, `name` | Stable node identity and separate provider display name |
| `image`, `server_type`, `location` | Approved root-login image, machine size and compatible location |
| `ssh_key_ids`, `ssh_key_id` | Existing registration ID list and optional primary reference |
| `firewall_ids`, `network_id` | Prepared shared firewall ID list and network ID |
| `private_ip` | Optional desired private address (null delegates allocation); validate subnet membership and uniqueness |
| `labels` | Non-secret machine labels |

Semantic input validation belongs to the library integration, not placeholder
substitution: check CIDRs, unique private IPs, location/network-zone
compatibility, provider limits, nonempty SSH access, required firewall IDs,
public-key ownership and approved root-login images before mutation.
The public interface has IPv4 enabled and IPv6 disabled, matching the existing
ONCE/ClickHouse shape. Alternate image login users and IPv6-only nodes need an
explicit extension; they are not silently inferred.

## Ownership and workflow

Shared state `<profile>/compute/shared.tfstate` owns:

- `hcloud_network.network`
- `hcloud_network_subnet.network`
- `hcloud_firewall.network`
- optional `hcloud_ssh_key.machine`

Shared `params` returns provider, vpc_id, vpc_ip_range, subnet_id, subnet_cidr,
and firewall_id. The optional key fragment exposes separate `ssh_key_id`.
Complete shared preparation before dispatching nodes, including subnet
creation; an available network ID alone is not proof the subnet exists.

Each `<profile>/compute/nodes/<node_id>.tfstate` owns `hcloud_server.node` and
`hcloud_server_network.node`. The server receives the shared firewall IDs
in its creation configuration. The shared firewall contains no `apply_to`
selector or duplicate attachment owner. Private network attachment depends
on the server, and `params.vpc_ip` references that attachment, so the output
cannot complete before the attachment. The join must still validate real
connectivity and SSH readiness before application convergence.

Node `params` contains node_id, provider, observed name, public ip, vpc_ip,
server_id, vpc_id, network_attachment_id, user, sudoer and ssh_key_id.
Login and sudoer are root for the supported image contract. No private key
content is read or returned; role/index and identity-path references are
orchestration metadata. No SSH provisioner is embedded in these templates.

The runtime passes `HCLOUD_TOKEN` from `COLORS_PAR_HCLOUD_TOKEN`; no token is
rendered. Backend credentials and configuration are separate library concerns.
Destroy must remove all nodes/attachments before shared network, firewall and
key ownership is removed.

## Explicit capability limits

Hetzner Cloud Firewalls do not filter private Cloud Network traffic, according
to the [official firewall FAQ](https://docs.hetzner.com/cloud/firewalls/faq/#can-firewalls-secure-traffic-to-my-private-hetzner-cloud-networks).
Therefore `public_rules` cannot enforce per-peer private restrictions. The
owned network establishes deployment membership but does not filter members'
east-west traffic. A request for provider-enforced private port/peer filtering
must be rejected before provisioning; do not substitute guest firewall rules
and claim the same provider capability. Guest firewalls may add application
protections independently.

The example admits public SSH, a declared UDP service and ICMP from a restricted
source; it opens no public database port. With no outbound rules, Hetzner allows
outbound traffic, also documented in the [official FAQ](https://docs.hetzner.com/cloud/firewalls/faq/#how-do-the-firewalls-work).
Explicit outbound requirements must supply suitable public rules and account
for provider-reserved traffic; full traffic isolation is not claimed.

Discovered/shared-with-other-deployments networks, role-specific firewall
profiles, placement groups, volumes, dedicated servers and load balancers are
not implemented in this milestone. Existing ClickHouse uses static private
addresses and separate access/network/server/firewall states; migration must
preserve those identities explicitly, including `hcloud_server.node1` becoming
`hcloud_server.node` and old network/firewall/key addresses. The firewall
attachment ownership change requires semantic review and a replacement-free
plan. No state move or package migration is performed by these templates.

## Validation and handoff

```sh
python3 colors-compute/providers/hcloud/check.py
python3 colors-compute/providers/hcloud/check.py --tofu /absolute/path/to/tofu
```

Verified 2026-09-09 with OpenTofu 1.12.5 and exact pinned provider hcloud 1.54.0:
`fmt -check`, `init -backend=false`, and `validate` passed for shared keygen,
shared opt-out and node examples. The downloaded provider signature verified
with key 5219EACB3A77198B. Initialization ran in temporary directories with
credential environment variables excluded; no plan/apply/live state operation
ran. Deterministic checks passed for typed substitution, example bytes,
creation-time firewall IDs and attachment-derived private outputs.

Provider schema acceptance does not prove regional image/size availability,
firewall timing, boot/login behavior, private routing, locks or safe migration.
Release packaging still needs reviewed multi-platform provider locks.

Handoff: adapter templates/examples are complete and schema-validated. Remaining
work is semantic validation and capability refusals, remote-state/shared
ownership lifecycle, SSH/network readiness, ClickHouse migration and runtime
integration. The private-firewall limitation must remain explicit in the
provider registry and capability checks before package adoption.
