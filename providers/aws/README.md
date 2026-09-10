# AWS compute render contract

These library-owned JSON templates separate shared AWS infrastructure from one
EC2 node. They are declarative adapter assets, not a lifecycle runner. They
contain no application configuration, backend configuration, or AWS credentials.
The provider uses the ambient AWS credential chain at runtime.

Parse templates as JSON and render with the library `render_template` operation.
Whole-string `{{key}}` placeholders preserve JSON types. Terraform `${...}`
expressions remain intact. See [the rendering contract](../../contracts/rendering.md).

## Shared and node configurations

Render `shared.tf.json.template` to shared-state `main.tf.json`. It owns one
VPC, public subnet, internet gateway, route table, default route, route table
association, security group, and stable-keyed ingress/egress rules. Shared
outputs expose the VPC, subnet, and security group IDs for node requests.

In managed-key mode render `shared-keygen.tf.json.template` to `key.tf.json`
in the same shared configuration. It creates `aws_key_pair.machine` once with
the profile as its regional key name, and outputs `ssh_key_id` and `key_name`.
Opt-out omits this fragment and requires an existing operator-owned regional
key name as a node input. The node never registers or deletes that key.

Role-policy deployments select `shared-roles.tf.json.template` instead of the
homogeneous shared template. They create one security group per role and attach
observed peer `/32` ingress rules only to their declared target role; each node
uses its matching group. The shared output `role_firewall_ids` maps role names
to group IDs. Before nodes exist, peer ingress is omitted; after the node join,
the runtime reconverges shared rules with observed private addresses. It does
not grant blanket VPC ingress. Role groups allow outbound IPv4 traffic. See
[role requests](../../contracts/roles.md).

Render `node.tf.json.template` into each node's isolated state directory. It
owns exactly `aws_instance.node`; it has no resource count or shared resource
creation. Its outputs include stable node identity, observed Name tag, instance
ID, public/private IP, login user, sudoer, and key registration reference. New
Ubuntu deployments conventionally pass `ubuntu` for user and sudoer; both are
explicit inputs so validated image-specific users can be supplied. No private
key content or file reads appear in the configuration.

The library must finish shared apply successfully before dispatching nodes.
Passing shared resource IDs does not create cross-state Terraform dependency
edges: sequencing, ownership, locking, retries, and readiness belong to the
library runtime and are not implemented by these templates.

## Inputs

| Input | Type and meaning |
|---|---|
| `region`, `availability_zone` | AWS region and matching availability zone |
| `profile`, `name`, `firewall_name` | Validated deployment identity and resource display names |
| `vpc_cidr`, `subnet_cidr` | Canonical IPv4 CIDRs; subnet contained in VPC |
| `ingress`, `egress` | Maps keyed by stable rule identities; values contain `protocol`, `from_port`, `to_port`, `cidr` |
| `firewall_groups` | Role-to-group-name map, role shared configuration only |
| `role_ingress` | Stable rule map with target `role`, `protocol`, `from_port`, `to_port`, and IPv4 `cidr` |
| `public_key` | Validated public key content, managed-key shared fragment only |
| `node_id`, `image_id`, `instance_type` | Stable node identity, regional AMI ID, instance type |
| `subnet_id`, `security_group_ids` | Resolved shared subnet ID and security group ID list |
| `key_name`, `ssh_key_id` | Existing shared/opt-out regional key name and downstream registration reference |
| `root_volume_size_gb` | Positive integer; encrypted gp3 root disk size |
| `user`, `sudoer` | Validated image-specific SSH login and privilege identities |
| `prevent_destroy` | Boolean node lifecycle protection; true by default |

For protocol `-1`, rule port values must be null. Named TCP/UDP rules use numeric
ports. The adapter currently supports IPv4 CIDR sources/destinations only;
IPv6, security-group source references, existing network discovery, multiple
subnets, private-only nodes, and additional volumes need explicit extensions.
There are no hardcoded application ports or default ingress rules. Examples
show SSH restricted to a documentation CIDR and a private peer rule as data.
Homogeneous-template egress is supplied as data. The role template permits all
outbound IPv4 traffic for each role group.
Standalone security group rules are deliberately used without inline rules,
as the [AWS provider documentation](https://registry.terraform.io/providers/hashicorp/aws/5.36.0/docs/resources/vpc_security_group_ingress_rule)
warns against mixing the two ownership models.

The runtime must validate CIDRs, rule ports/protocols, names, region consistency,
key mode, AMI availability and user compatibility before mutation. Render and
schema validation alone do not establish those conditions. The example AMI,
subnet, and security group IDs are placeholders, not deployable defaults.

## Migration limits

ONCE's legacy `aws_instance.node1`, `aws_security_group.node1`, and
`aws_key_pair.operator` addresses differ from these shared/node addresses.
Its inline route and security-group rules also require explicit ownership
mapping. Existing opt-out templates registered public-key file content under
a package name; selecting an existing key name in this adapter is therefore
an access/ownership migration, not a transparent setting translation.
Encrypted root volumes and standalone firewall rule resources require reviewed
plans before adoption. Do not migrate by changing dependency pins alone or by
applying these examples to an existing state.

Provider Name tags remain observed outputs. Changing a Name tag does not repair
a guest hostname. Image, network, and other changes may replace an instance;
`prevent_destroy` remains explicit. Readiness and application installation are
separate downstream operations; these templates have no SSH provisioners.

## Validation

```sh
python3 colors-compute/providers/aws/check.py
python3 colors-compute/providers/aws/check.py --tofu /absolute/path/to/tofu
```

The checker verifies deterministic representative renders, typed fields, and
single-node/shared ownership separation. The second command additionally runs
`fmt -check`, `init -backend=false -input=false -no-color`, and
`validate -no-color` for five configurations: shared keygen, shared opt-out,
role shared keygen, role shared opt-out, and node.
Each runs in a temporary directory with an isolated HOME, an allowlisted
environment without cloud credentials, and AWS metadata disabled. No plan,
apply, destroy, account query, or state access runs.

All checks passed on 2026-09-09 with OpenTofu 1.12.5 and exactly pinned
`hashicorp/aws` 6.31.0. OpenTofu verified the provider signature with key
`0C0AF313E5FD9F80`. Provider caches and generated locks were temporary. Packaging
must still establish reviewed dependency-lock distribution.

These schema checks prove configuration/provider-schema compatibility only.
The separate live Green validation below additionally exercises one AWS
configuration through the shared compute runtime.

## Live AutoMQ validation — 2026-09-10

The Green AutoMQ package successfully created an AWS deployment with profile
`automq-aws` using colors-compute
`87ec5661fc8807159419d90d437247f3503469c7`. The test used `us-east-1a`, three
`t3.large` instances, Canonical Ubuntu 24.04 AMI `ami-025d99823a4caad37`,
40 GiB encrypted gp3 root disks, VPC `10.73.0.0/16`, and subnet
`10.73.1.0/24`. Each node used the library-owned regional SSH keypair and the
image's `ubuntu` login with sudo privileges.

The deployment created the library's shared network and security resources,
then ran three copies of the same node operation through Green fan-out and
join. A deployment-owned S3 backend was bootstrapped before remote-state reads
and used for the conditional compute journal and separate shared/node states.
AutoMQ separately created its S3 data/ops buckets and scoped IAM identity.
DNS was disabled for this test; clients used public IPs and a deployment-owned
private certificate authority.

The completed create passed the package's on-host and external acceptance
stages, including record round trips, object-store checks, authentication and
ACL refusals, broker failover with pre-failure record survival, consumer-offset
preservation, controller re-authentication, and a producer workload. This
exercises the configured public client and private cluster paths; it is not a
throughput benchmark or a production availability assessment.

Live execution exposed and fixed two compute integration issues: Green's
coordinator tried to serialize workflow callbacks, and OpenTofu returned a
synthetic initial state with an empty lineage on a confirmed-absent backend.
The first failed shared attempt was recovered only after an explicit,
operation-bound library recovery confirmed absent state and zero matching AWS
VPCs, subnets, gateways, route tables, security groups, and keypairs.

The first successful create used local checkouts. AutoMQ source was published
as `e3beeaf08472fc3ab4e5121eca876e1f0bb6f8e9`, with copied launcher pins published
as `debb50b6bcadd38ab14efa91fe325d79623cad9c`. A second complete create using
those immutable pins, with no working-tree overrides, passed all stages and
16 external acceptance gates with zero failures. An independently produced
50-record sentinel remained byte-for-byte identical across reconvergence
(SHA256 `5cc2ef77184dc7ff31424088d6a7bd955906bd6cfd284cb7f34631230c26e04a`).

During the published-pin acceptance run, a controlled stop of node 2 left its
partition writable within one second after the stop command completed. All
100 exact pre-outage records survived, the broker rejoined with zero lag,
per-partition consumer offsets were preserved, and restarting controller 1
successfully exercised re-authentication. These are observed outcomes of this
single test, not availability guarantees.

A delete attempted without disabling the protection guard refused with exit 2
before destruction. Authorized deletion then passed using the published launcher
without working-tree overrides. All three AutoMQ containers were independently
confirmed absent before removing application storage. The package deleted its
S3 buckets and IAM user, then compute destroyed nodes before shared resources,
removed the owned local SSH keypair, retired its journal, and finalized the
managed S3 backend. The profile SSH aliases were also confirmed absent. A second delete passed through only the
start and backend-finalize stages, with the backend already absent.

The final independent inventory checked the recorded resource IDs, including
all three root EBS volume IDs and the VPC's implicit routes, security groups,
network interfaces, and network ACLs. It reported zero remaining resources,
zero remaining billable resources, and all recorded volumes absent. Evidence
is retained in the deployment's `evidence/resources-after-delete.json`. The
observed destroy sequence took approximately 129 seconds for host cleanup,
15 seconds for application storage, 302 seconds for compute, and 24 seconds
for backend finalization; these are one-run observations.

Blue and Red passed their automated suites and cross-color parity, but have
not been exercised live in this validation. External-key adoption, other AMIs
or regions, interrupted AWS applies, and legacy state migration remain outside
this run's evidence. The supported IPv4/network scope and replacement guards
above still apply.

## Role-firewall validation — 2026-09-10

Commit `09ec539e75dc21c4dafb019eb8f9da276e695f6f` publishes AWS role
firewalls in all three colors. Role-specific instance overrides such as
`aws-instance-type-app` and `aws-instance-type-clickhouse` use the existing
role request contract. The added fixtures exercise distinct role sizing,
node-to-role security-group attachment, and exact peer `/32` ingress.

The five configurations above passed the checker with OpenTofu 1.12.5 and AWS
provider 6.31.0. The complete library suites passed: Blue 511 tests, Green 107
tests with 1,331 assertions, Red 316 tests plus typecheck, and 478 parity cases
in each color. These are offline render, contract, and provider-schema checks.

Langfuse's AWS integration using this published compute pin passed local build
and create dry-run checks for profile `langfuse-aws`: six nodes across app,
Neon, ClickHouse, and Redis roles, role-specific instance sizes, encrypted
60 GiB root disks, role security groups, and managed S3 state configuration.
The application owns its separate S3 storage lifecycle. This validates package
integration offline; no Langfuse AWS live success is claimed. Live execution
was pending a Cloudflare token at this handoff. The completed AutoMQ lifecycle
above used the homogeneous shared configuration and does not establish live
role-firewall behavior.
