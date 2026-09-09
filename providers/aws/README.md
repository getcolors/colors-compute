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
Egress is also supplied as data, rather than implicitly allowing all traffic.
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
`validate -no-color` for shared keygen, shared opt-out, and node configurations.
Each runs in a temporary directory with an isolated HOME, an allowlisted
environment without cloud credentials, and AWS metadata disabled. No plan,
apply, destroy, account query, or state access runs.

All checks passed on 2026-09-09 with OpenTofu 1.12.5 and exactly pinned
`hashicorp/aws` 6.31.0. OpenTofu verified the provider signature with key
`0C0AF313E5FD9F80`. Provider caches and generated locks were temporary. Packaging
must still establish reviewed dependency-lock distribution.

Schema checks prove configuration/provider-schema compatibility only. Live
permissions, AMI boot/login behavior, network traffic, remote state locking,
shared ownership, retry/delete behavior, and replacement-free migration remain
unverified. No AWS resources were created.
