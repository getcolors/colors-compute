# Local compute backend

Compute OpenTofu state lives at
`<SDK workdir>/<profile>/<node_id>/<state_filename>` with native state locking.
Provider registrations use their own `registration-<name>` directory and state.
No object-storage credentials or key objects are required by local compute.

SSH authority is an independent encrypted resource, described in
[SSH resources](ssh-resource.md). Compute and registration state contain only
public SSH identity and its reference. They contain no TLS key generation,
private-key output, passphrase, or disposable decrypted access copy.

Inspect and delete require readable state. A first create may start without state
unless `compute-require-existing-state` is enabled. Existing state identity and
backend cannot be rebound. Changing a state root never moves resources.

Deletion retains templates and initialization files and does not delete the SSH
resource. Keep state and saved plans private: provider state can contain other
sensitive infrastructure values even though SSH private material is absent.
