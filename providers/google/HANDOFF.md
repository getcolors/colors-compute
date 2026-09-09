# Google provider handoff

Complete: shared network/subnetwork/data-driven ingress template; single-node
instance plus static public IPv4 template; deterministic example renders;
credential-free schema checker; input, ownership and access documentation.
Both templates pin hashicorp/google 6.0.0 and use ambient ADC without credential
configuration. Key modes share supplied public-key content with ubuntu login;
there is no key registration or private-key/file read.

Validation performed with OpenTofu 1.12.5: deterministic checks, fmt -check,
backend-disabled init, and validate passed for shared and node configurations.
Provider signatures verified; all initialization files stayed in temporary
folders. No plan/apply/destroy, live resources, commits, or pushes.

Remaining parent work: cross-color asset packaging and parity, runtime semantic
validation, resolved shared references, isolated remote state, protected backend
credentials, readiness checks and package adoption. OS Login-required projects,
custom image login users, restricted egress and deny firewall rules are outside
this first adapter. Existing state/address/access behavior needs explicit
migration review. README records these limits and source links.
