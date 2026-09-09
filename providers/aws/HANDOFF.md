# AWS adapter handoff

Implemented shared VPC/subnet/gateway/routes/security group and rule templates,
optional shared key registration, and exactly one EC2 instance per node template.
Added deterministic keygen, opt-out, and node examples plus a schema checker.
Provider version is pinned to `hashicorp/aws` 6.31.0.

Validation completed successfully with OpenTofu 1.12.5:

- Deterministic renders and shared/node ownership checks.
- `fmt -check` for all three configurations.
- `init -backend=false -input=false -no-color` for all three configurations.
- `validate -no-color` for all three configurations.

All commands ran in temporary configurations with isolated HOME, no credential
environment, and metadata disabled. Provider signature verified. No cloud plan,
apply, destroy, credentials, live state, or AWS API calls were used.

Remaining work: semantic input validation, package resource distribution,
shared/node lifecycle, remote ownership and lock behavior, SSH readiness, and
reviewed legacy state/key-mode migration. See README for the supported IPv4
network scope and replacement/ownership migration limitations. No commits or
pushes were performed by this agent.
