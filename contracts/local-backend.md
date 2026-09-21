# Local state backend

OpenTofu runs in the persistent `<SDK workdir>/<profile>/<node_id>` directory,
with state at the caller-provided filename and native state locking. No journal,
remote key store, AWS key provider, or S3 credentials are required.

`tls_private_key.machine` owns the node's ED25519 pair in local state. OpenTofu
outputs supply the pair for access preparation; the private output is sensitive.
The runtime verifies the pair and state fingerprint and overwrites private local
access files; those files are never adopted as state or used to generate keys.
Private key outputs are never returned by the library's public API or diagnostics.
`ssh-s3-*` options are rejected for this backend.

A first create can start without state. Existing key files without readable state
require recovery; they must not be replaced with a new identity. Inspect, access
preparation, and delete require readable state. Successful destroy removes the
TLS resource and local access copies, while retaining templates and `.terraform`.

Changing a state root does not move resources or transfer ownership. State files,
backup files, and saved plans contain secrets and require private permissions
and reviewed recovery after interruption. Retained backups can contain old keys.
