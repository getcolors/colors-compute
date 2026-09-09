# Yandex provider handoff

Complete: shared network/subnet/security group with data-driven ingress and
egress; one instance and reserved IPv4 per node; normalized public/private
outputs; common public-key metadata path for generated and opt-out modes;
examples, deterministic checker and documented ownership/input limitations.

Validated both configurations with actual OpenTofu 1.12.5 fmt -check,
init -backend=false and validate. Provider yandex-cloud/yandex 0.120.0 pinned and
signature-verified. No plan, apply, destroy, credential reads or commits/pushes.

Remaining: runtime YC_TOKEN binding, semantic input validation, shared reference
resolution, isolated state and remote locks, SSH readiness and package migration.
Image family lookup must resolve an immutable ID before rendering. Existing
unreserved NAT, default security-group access and image-family behavior require
explicit migration review. Only the dedicated IPv4 security group and Ubuntu
metadata-key access path have templates here. README contains detailed limits.
