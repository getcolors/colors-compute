# AWS adapter handoff

The shared VPC/subnet/gateway/routes/security-group templates, optional shared
key registration, and one-EC2-instance node template are implemented and
packaged in all three colors. The provider is pinned to `hashicorp/aws` 6.31.0.
Shared/node lifecycle, conditional ownership, SSH key preparation, and remote
state operations are implemented by colors-compute rather than the templates.

## Schema validation

OpenTofu 1.12.5 passed deterministic renders, shared/node ownership checks,
`fmt -check`, backend-disabled initialization, and validation for keygen,
external-key, and node configurations. The role-firewall update additionally
validated role shared configurations in managed-key and external-key modes,
for five configurations total. The original 2026-09-09 schema checks
used isolated temporary configurations with no cloud credentials or AWS calls.
These checks were repeated successfully during the live investigation.

## Green live validation — 2026-09-10

AutoMQ profile `automq-aws` successfully created three Ubuntu 24.04 `t3.large`
nodes in `us-east-1a` through colors-compute
`87ec5661fc8807159419d90d437247f3503469c7`. The deployment used managed SSH
registration, a managed S3 backend, and separate shared/node state and journal
objects. AutoMQ owned its additional S3 data/ops buckets and scoped IAM user.
All on-host and external acceptance stages passed, including authenticated
traffic, ACL refusals, record survival across broker failover, consumer offsets,
and controller restart/re-authentication.

The investigation fixed Green workflow-callback serialization in coordinator
construction and strict handling of OpenTofu's synthetic empty initial state.
An explicit library recovery checked the failed operation ID, absent shared
state, and zero matching AWS shared resources before permitting another create.
No state was silently reseeded and the prepared SSH key was preserved.

AutoMQ source `e3beeaf08472fc3ab4e5121eca876e1f0bb6f8e9` and launcher commit
`debb50b6bcadd38ab14efa91fe325d79623cad9c` publish the tested work. After the first
success with local checkouts, a complete reconvergence using only those
published pins passed all stages and 16 external gates with zero failures.
A separate 50-record sentinel remained byte-for-byte identical across that
reconvergence. The controlled node-2 outage preserved all 100 exact pre-outage
records and per-partition offsets, recovered writes within one second after
the stop command completed, and returned the broker at zero lag. Controller 1
also re-authenticated after restart.

Deletion with protection enabled refused with exit 2 before destruction.
Authorized full deletion then passed using the published launcher without
working-tree overrides. All three containers were confirmed absent before
application storage removal. Application buckets/IAM identity, EC2 nodes,
shared networking, the owned local SSH keypair and aliases, and finally the
managed state bucket were removed. The compute journal retired before
backend finalization. Repeat delete also passed, visiting only start and
backend-finalize with the backend already absent.

The independent final inventory verified exact recorded root-volume IDs and
VPC dependencies, including implicit defaults and network interfaces. It
reported `remaining_resource_count=0`, `remaining_billable_resource_count=0`,
and `all_recorded_volumes_absent=true`. The deployment retains the result in
`evidence/resources-after-delete.json`. This completes one live Green
create/reconverge/guarded-delete/delete/repeat-delete lifecycle.

Compute validation at the completed AutoMQ handoff passed Blue 510 tests,
Green 107 tests with 1,327 assertions, Red 314 tests plus typecheck, and 476
parity cases in each color.
Blue/Red tests are not Blue/Red live deployment evidence. See [README](README.md)
for the exact AWS configuration and [managed backend ownership](../../contracts/managed-backend.md)
for bootstrap/finalization and interrupted-purge behavior.

Remaining verification includes other regions/images, external-key adoption,
interrupted AWS applies, and reviewed legacy-state migration. The IPv4 network
scope and replacement guards remain as documented in README.

## AWS role firewalls and Langfuse offline integration — 2026-09-10

Published compute commit `09ec539e75dc21c4dafb019eb8f9da276e695f6f` adds
AWS role shared stages in all three colors. Each role owns a security group;
nodes attach their role's group, and declared peer edges expand to observed
private `/32` addresses on the shared convergence after node join. Peer ingress
is absent before addresses exist, with no blanket VPC ingress rule. Each role
group allows outbound IPv4 traffic. Managed-key and external-key role variants
use the same existing shared/node lifecycle and ownership contracts.

Validation passed all five AWS provider-schema configurations, Blue 511 tests,
Green 107 tests with 1,331 assertions, Red 316 tests plus typecheck, and 478
parity cases in each color. New fixtures check role sizing, group attachment,
and exact peer rule isolation.

Langfuse's integration using this compute pin passed local deployment build
and create dry-run checks for six AWS nodes, role-specific sizes, encrypted
60 GiB root disks, role security groups, and managed S3 state configuration.
This is offline evidence only: no Langfuse resources were provisioned, and
live execution was awaiting a Cloudflare token at this handoff. AutoMQ's
completed Green live lifecycle above remains the live evidence for the
homogeneous AWS configuration. A live role-policy create/converge/delete
cycle, including private peer connectivity, still needs verification.
