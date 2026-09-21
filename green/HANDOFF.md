# Green single-node compute implementation

`io.github.getcolors.compute-node` implements `node-plan`, `build-node!`, and
`compute-node!`. The canonical behavior is in `../contracts/node.md`.

Each SDK request selects one persistent `<workdir>/<profile>/<node_id>` root and
one state filename. The combined OpenTofu module contains the node, its owned
supporting resources and an ED25519 keypair. Remote backends own authoritative
S3 key objects; the local backend owns its authoritative keypair directly in
local OpenTofu state and requires no S3 provider.
OpenTofu runs in that root without `TF_DATA_DIR`. Templates and initialization
files remain after destruction. Ansible receives only a freshly downloaded,
verified local copy of the authoritative keypair.

The library no longer contains deployment orchestration, journals, coordination,
managed backends, topology expansion, or workstation-owned key lifecycle APIs.
Provider template rendering, provider validation, private filesystem helpers,
and exact-environment process handling remain reusable internal foundations.
There is no Colors SDK dependency: the caller supplies the SDK workdir and
owns orchestration.

Run `bb test` from this directory. Tests cover all eight providers, native
process handling, remote object absence checks, state identity/refusal guards,
replacement and destruction protection, local key overwrite/failure, and
persistent templates through create/delete. No cloud operations are exercised.

Runtime failures return bounded structured diagnostics with an authored code,
stage, message, and infrastructure-change risk. Safe command prefixes, resolved
executables, exit codes, and redacted stderr explain launcher failures without
returning stdout, state, plans, or private keys. The risk becomes `possible`
immediately before apply and remains so through all subsequent failures.
