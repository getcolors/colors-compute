# DigitalOcean compute render contract

These library-owned JSON templates provide the machine/network portion of
PostgreSQL and MySQL cluster provisioning. They discover the regional default
VPC, prepare a deployment firewall and tag before node fan-out, optionally
register one managed SSH key, and provision one droplet per node state.
They contain no database, backup, DNS or application configuration.

## Rendering and inputs

The substitution contract matches the Vultr adapter: parse JSON and recursively
replace exact `{{name}}` strings with a deep copy of the named input, preserving
its JSON type. Reject missing inputs or partial placeholders. Leave `${...}`
OpenTofu expressions unchanged. `check.py` is an executable reference check.

| Input | Meaning |
|---|---|
| `profile` | Safe deployment identity and managed SSH registration name |
| `region` | DigitalOcean region; discovers its existing default VPC |
| `deployment_tag` | Unique, validated deployment-owned firewall attachment tag |
| `firewall_name` | Validated firewall display name |
| `public_ingress` | Stable-keyed map of protocol, optional port_range, source_addresses |
| `private_ingress` | Stable-keyed map of protocol and optional port_range; sources are the discovered VPC CIDR |
| `outbound_rules` | List of protocol, optional port_range, destination_addresses |
| `public_key` | Managed public key content, never private key material |
| `prevent_destroy` | Literal boolean, true by default |
| `node_id`, `name` | Stable node identity and separately resolved machine name |
| `size`, `image` | DigitalOcean droplet size and approved image slug |
| `vpc_id` | VPC ID returned by completed shared preparation |
| `ssh_key_ids`, `ssh_key_id` | Existing key references and optional primary reference; null permitted for primary reference in opt-out mode |
| `tags` | Existing application tags plus the required shared deployment tag |

Write `shared.tf.json.template` into the shared configuration. Add the
`shared-keygen.tf.json.template` fragment in keygen mode only; omit it in
opt-out mode. Its resource has no count, so its stable address is
`digitalocean_ssh_key.machine`. Changing modes requires ownership migration.
The shared key output is `ssh_key_id`, separate from shared `params`.

Write `node.tf.json.template` into each isolated node configuration. Shared
preparation must finish before nodes are dispatched. No node branch creates
or adopts a VPC, firewall, tag or key registration.

Runtime validation must check CIDRs, protocol/port rules, nonempty SSH access,
tag ownership/collisions, region consistency, image/login support, managed key
validity, and presence of the shared tag on every node. Placeholder substitution
is not semantic validation. The current node contract uses `root` for both
user and sudoer and therefore requires a supported root-login image.

## Network trust and ownership

Shared state is `<profile>/compute/shared.tfstate`. Canonical addresses are:

- `data.digitalocean_vpc.network`: discovered network; never owned or destroyed.
- `digitalocean_tag.deployment`: deployment-owned attachment identity.
- `digitalocean_firewall.network`: deployment-owned provider firewall.
- `digitalocean_ssh_key.machine`: optional deployment-owned access registration.

The firewall targets `tags = [digitalocean_tag.deployment.name]`, not a list
of future droplet IDs. Nodes receive that tag at creation. This removes the
configuration dependency cycle and lets the firewall exist before nodes.
Actual propagation/attachment timing still requires live verification; schema
validation does not establish an atomic network boundary at droplet boot.
No other deployment may reuse this tag. Lifecycle checks must verify firewall
attachment before application convergence and retain shared resources until
all owned nodes have been destroyed.

Public ingress sources are explicit. Private ingress derives its source from
the regional default VPC's actual CIDR. This preserves the database packages'
existing trust boundary: every host in that VPC can reach permitted private
ports, including hosts outside this deployment. Isolated deployment networking
is not implemented here; an isolation requirement must fail rather than be
silently weakened. The examples allow private TCP/UDP and ICMP as the existing
cluster templates do, with restricted public SSH and database client ports.

Node state is `<profile>/compute/nodes/<node_id>.tfstate`, owning exactly
`digitalocean_droplet.node`. Its `params` contains node_id, provider, observed
name, public ip, vpc_ip, droplet_id, vpc_id, user, sudoer and ssh_key_id.
Role/index and access-path references belong to orchestration and the join.

No token is rendered. The runtime passes `DIGITALOCEAN_TOKEN` from
`COLORS_PAR_DO_TOKEN`. Backend configuration and credentials are owned by the
library's separate remote-state layer. These templates never read local keys
or execute SSH provisioners.

## Migration limits

Existing database states own one firewall targeted by droplet IDs and counted
node resources. Migration must map those addresses and node identities
explicitly, preserve existing names and tags, and review the switch from ID
attachment to deployment-tag attachment. This switch is a semantic firewall
change, not merely an address move. No automatic migration is implemented.

MySQL's reserved IP is not implemented in this adapter milestone. A later
library extension must own the address while preserving application-controlled
assignment: no desired `droplet_id`, and `ignore_changes = [droplet_id]` as in
the existing MySQL packages. MySQL package migration cannot be declared complete
without that extension and its ownership/recovery checks. Owned VPC creation
and managed Kubernetes are also outside this adapter milestone.

## Validation and handoff

```sh
python3 colors-compute/providers/digitalocean/check.py
python3 colors-compute/providers/digitalocean/check.py --tofu /absolute/path/to/tofu
```

Verified 2026-09-09: OpenTofu 1.12.5 successfully ran `fmt -check`,
`init -backend=false`, and `validate` for shared keygen, shared opt-out and
single-node examples. The DigitalOcean provider is pinned exactly to 2.51.0,
matching the existing MySQL package; the downloaded provider signature was
verified with key F82037E524B9C0E8. Initialization and lock files stayed in
temporary directories. Release packaging still needs reviewed multi-platform
provider lock distribution.

Deterministic checks compare all rendered example bytes, verify typed
substitution and preserved expressions, reject missing/partial placeholders,
and assert tag-based attachment, absence of an owned VPC, and node tag presence.
No apply, plan, credential access or live state operation ran. Provider schema
validation does not verify account API behavior, regional discovery, firewall
traffic, tag propagation, image login, remote state, or replacement-free plans.

Handoff: canonical templates and examples are complete for discovered VPC,
shared tagged firewall/key and one node. Remaining integration work is semantic
validation, ownership lifecycle, remote-state orchestration, SSH readiness,
state migration, provider-lock packaging and the MySQL reserved-IP extension.
