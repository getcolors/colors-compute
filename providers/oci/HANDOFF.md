# OCI adapter handoff

Implemented shared discovery of the existing subnet/VCN, owned NSG and generic
stateful IPv4 rules, and one flexible-shape OCI instance per node configuration.
Added separate pinned-image and shape-matched Ubuntu 24.04 discovery fragments,
representative renders, schema checker, and capability/migration documentation.
Provider pin: oracle/oci 8.4.0. Runtime auth selects config_file_profile from the
ambient OCI config; templates contain no credentials or private key reads.

All three configurations passed deterministic checks, fmt -check,
init -backend=false, and validate using OpenTofu 1.12.5. Signed provider key:
1533A49284137CEB. Temporary HOME had no OCI config; no plan, apply, destroy,
remote state, or account operations ran.

Remaining runtime requirements: validate existing subnet public routing and
trust boundary; enforce unsupported strict-isolation refusal because NSG rules
cannot narrow existing security-list grants; record/pin discovered image;
implement remote ownership, lifecycle sequencing, SSH readiness, semantic
validation, and reviewed legacy state migration. No commits/pushes by this agent.
