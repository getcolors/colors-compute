# Shared local SSH key lifecycle

The library owns local deployment key preparation and cleanup. Provider-side
registrations remain shared-resource lifecycle work, not one registration per
node. This API neither creates cloud registrations nor grants dispatch authority.

`prepare_keypair(opts, ownership, environment, record_intent, record_prepared,
runner)` uses provider registry `ssh-setting` presence to select mode. An absent
setting selects managed keygen. A present valid nonblank string or nonempty list
of nonblank strings/positive integer IDs selects external mode and returns
`{mode:"external",setting:<registry setting>,reference:<unchanged reference>}`.
An optional nonblank `ssh-private-key-path` remains an identity path reference.
Present null/blank/REPLACE_ME or invalid references are errors, never keygen.
External mode touches no files and invokes no ownership callback.

Managed ownership is exactly `{status:"fresh"}`, `{status:"prepared",
fingerprint:"SHA256:..."}`, or `{status:"error"}`. Fresh means the caller has
established no deployment key/nodes exist and owns the coordination lock; it
must not be inferred from a failed backend read. Error and invalid ownership
refuse before filesystem mutation. Existing local files with fresh ownership
are never adopted. Prepared ownership requires a complete matching local pair
whose public fingerprint equals the record; missing/inconsistent keys refuse.

Managed keys live at HOME/.ssh/profile and HOME/.ssh/profile.pub, using HOME
first then runtime home. Profile uses the shared safe-ID grammar. Reject
symlink key paths or a symlink SSH directory. SSH directory permissions are0700,
private key0600, public key0600. Never change external key permissions or files.

For fresh preparation, check collisions, invoke `record_intent()` and require
literal true before invoking ssh-keygen. Generate ed25519 without a passphrase,
comment `<profile> managed by Colors`, using argv without a shell. Derive the
public key from the private key using ssh-keygen -y, verify the public file,
then invoke `record_prepared(fingerprint)` and require literal true. Return only
`{mode:"managed",private_key_path,public_key_path,public_key,fingerprint}` after
that acknowledgement. Private content never enters library values or callbacks.
Existing prepared keys are verified/reused without generating or recording anew.

Callback refusal, exception, cancellation, or keygen failure preserves any
created files. The next fresh attempt refuses adoption; recovery requires
explicit ownership reconciliation. Diagnostics never include callback/process
text or private content. No library callback failure authorizes key removal.

`cleanup_keypair(opts,ownership,authority,environment,runner)` requires explicit
`{all_resources_destroyed:true}` for managed files. This is the caller's assertion
that all recorded nodes, attempted/retired resources, and every owned provider
key registration have been destroyed. Ownership must be prepared when files
remain. Verify remaining public/private material against the recorded fingerprint
before removing only these owned files; interrupted partial cleanup and repeated
cleanup are idempotent. Optionally include `known_hosts_owned:true` to remove
only the deployment's profile.known_hosts file. Never delete the SSH directory.
External mode never removes files. Uncertain destruction or ownership refuses.

Build/dry-run inputs return deterministic managed placeholders without touching
files/callbacks; cleanup in those modes returns a planned result. Explicit
non-create events cannot prepare keys, and non-delete events cannot clean up.
Language-specific event/dry-run keys follow their Colors SDK conventions.

Native runner injection follows the backend runner interface: argv, cwd, exact
environment, timeout milliseconds. Fixed SSH operations use a30-second timeout.
Public fingerprints are OpenSSH SHA256 over the decoded ed25519 public blob.
Tests must use isolated temporary HOME, actual ssh-keygen, callback failures,
collisions, prepared missing/mismatched keys, strict authority, opt-out non-touch,
symlink refusals, and repeated/partial cleanup. Successful local tests do not
establish cloud key-registration ownership or deployment lock correctness.

Real managed preparation reserves the workstation profile across processes with
CREATE_NEW on `.ssh/.<profile>.colors-key.lock`, mode0600. Directory setup and
the local reservation precede the intent callback; intent still must commit
before key generation. The reservation spans collision checks, generation,
verification and prepared acknowledgement. A competing or stale reservation
refuses explicit recovery and is never deleted by a process that did not create
it. Normal completion/cancellation removes only its own reservation. This
protects same-profile deployments even when their remote backend locks differ.
