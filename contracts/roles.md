# Role requests and peer firewall convergence

Applications declare topology and connectivity, never provider resource mappings.
Existing homogeneous requests are unchanged. Named nodes carry their topology
`role` into `provider_request` even when they share the default security policy.

`requirements.entry_node_id` optionally selects a declared node as the returned
cluster entry. The default remains the first expanded node. `requirements.roles`
is an optional map covering every declared role exactly; mixed unnamed nodes are
refused. Each value contains exactly `security`, using the existing policy shape.
For example:

```json
{
  "entry_node_id": "app-0",
  "security": {"ingress": [{"id":"ssh","protocol":"tcp","from_port":22,"to_port":22,"sources":["192.0.2.0/24"]}],"egress":"all","private_filter":true},
  "roles": {
    "db": {"security":{"ingress":[{"id":"ssh","protocol":"tcp","from_port":22,"to_port":22,"sources":["192.0.2.0/24"]},{"id":"database","protocol":"tcp","from_port":5432,"to_port":5432,"peer_roles":["app"]}],"egress":"all","private_filter":true}},
    "app": {"security":{"ingress":[{"id":"ssh","protocol":"tcp","from_port":22,"to_port":22,"sources":["192.0.2.0/24"]}],"egress":"all","private_filter":true}}
  }
}
```

An ingress rule contains exactly one of `sources` and `peer_roles`. Peer roles
must be unique declared roles. Each target role must retain public SSH ingress.
The assembly exposes `entry_node_id`, puts `role` on each named node request,
and copies the role policy map as `roles` into shared and node requests.

`compute-role-settings` is a desired-state map from declared role to optional
`size` and `image` values. Recipe `role_options` resolves these neutral fields to
provider settings. Explicit settings override the default provider options.
Recipe `role_size_legacy` lists historical provider option patterns, with
`{role}` replaced by the declared role; the first present value applies when
neutral `size` is absent. Unknown settings/roles and unsupported mappings refuse.
Role sizing works without role-specific firewalls. Azure image objects and
Yandex CPU/memory composite sizing are not covered by this scalar override.

The internal shared request may contain `peers`, a map from stable node ID to
exactly `{role,vpc_ip}` obtained from validated owned state or the completed node
join. Addresses must be canonical IPv4 strings. Rendered rule identity is
`<target-role>:<rule-id>:peer:<source-node-id>`, independent of the IP. Ordinary
CIDR rule identity remains `<target-role>:<rule-id>:<CIDR>`.

For role policy deployments, orchestration first converges shared network,
registration, role firewall groups and public ingress. On reconverge it preserves
observed surviving peer addresses read before key preparation. It then converges
the nodes, waits for every sibling, joins their verified outputs, and performs a
second guarded shared attempt containing exact `/32` rules from those outputs.
Only after the second shared attempt succeeds does application work receive a
ready result. If any sibling fails, no peer update runs and no ready result is
returned. The existing shared-state journal records both attempts, including
ambiguous failures. Delete removes nodes before the shared groups and rules.
Historical role removal that cannot be rendered safely refuses; it is not an
implicit ownership migration.

The initial adapter capability is Vultr. `shared-roles` and
`shared-roles-keygen` own `vultr_firewall_group.role[role]` and
`vultr_firewall_rule.ingress[stable-key]` in the existing shared state. Shared
`params.role_firewall_ids` selects the proper group for each single-node state.
There is no temporarily unfiltered node: groups and public SSH rules exist before
node creation. Other providers refuse the role firewall capability until their
library adapter supplies equivalent behavior. Vultr public rules also accept
canonical IPv6 CIDRs, preserving IPv6 Cloudflare ingress; private networks and
peer-address rules remain IPv4.

Build uses deterministic documentation addresses to render the final peer-rule
configuration without inspecting keys, credentials or live state. Changing an
existing homogeneous deployment to role groups changes resource ownership and
may require a reviewed migration; the normal replacement refusal still applies.
