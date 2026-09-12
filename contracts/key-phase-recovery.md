# Manual recovery of interrupted key phases

`intent` and `cleanup` deliberately refuse release. A process crash can also
leave a held lock after key phase `removed`. Neither elapsed time nor an
idle workstation proves that an owner stopped. These steps are an explicit
operator procedure, not an automatic retry or a lock takeover API. Use the same
profile, provider, backend bucket and region as the failed deployment.

## Establish exclusive recovery access

1. Stop the original invocation, its SSH/OpenTofu children, CI retries and every
   other writer for this deployment. Verify termination on the original machine
   and in the job runner. Disable scheduled launches during recovery. If any
   process or provider operation remains uncertain, leave the lock held.
2. Read `profile/compute/coordination.json` through `journal_get`, retain its
   opaque ETag or generation and save a private copy. Validate the schema-2
   document with `lifecycle_document_valid`. Compare its full identity with the
   deployment configuration, and record its held run ID and revision. Do not
   recover a different generation or identity.
3. Inspect every recorded state key. A transport error, missing bucket or
   unreadable state is not absence. Inspect provider resources and outstanding
   operations in the deployment account, region and project too. Include key
   registrations and resources whose create may have failed before writing
   state. Stop if ownership or the outcome is uncertain.
4. On the original workstation, inspect `HOME/.ssh/.profile.colors-key.lock`.
   Remove this reservation only after proving the process that created it and
   its children stopped. Check that its inode has not changed since inspection.
   Never remove another process's reservation. Reject symlinks and nonregular
   key files; do not follow or delete them.

## Reconcile intent

The document must have active status, key intent and no running or destroying
resource records. For this procedure, require every shared/node record declared
or destroyed and every corresponding state absent or strictly empty. Independently
confirm no deployment resources or key registrations survived at the provider.

For a managed complete pair, derive the public key with
`ssh-keygen -y -P '' -f HOME/.ssh/profile`. Compare it with `profile.pub`, then
obtain the OpenSSH SHA256 fingerprint. Verify that these files belong to the
interrupted run using its machine, exclusive file-creation history and logs.
A filename or matching public/private pair alone does not establish ownership.
After this review, the permitted journal change is key phase `prepared` with
that fingerprint. For external mode, set prepared with fingerprint null and
leave external files alone.

If no pair exists, or generation left only a partial pair, first prove which
files the failed run created. Move only those verified files to a private
quarantine outside the managed paths. Preserve foreign files and stop if any
file's ownership is uncertain. With no surviving resources or registrations,
the permitted repair is key `{mode:null,phase:"absent",fingerprint:null}`. This
repair is a manual journal correction, not a reducer transition. A later create
will run the usual collision checks and generate a new pair. Delete can retire
the absent-key generation without touching local files.

## Reconcile cleanup

Require deleting status and every shared/node record destroyed. Independently
verify all owned compute resources and provider key registrations are gone, and
that every recorded state is absent or strictly empty. Keep the journal held
while calling `cleanup_keypair` with event delete, the recorded prepared
fingerprint, and `all_resources_destroyed:true`. This function verifies remaining
managed key material before removal; an entirely missing pair is accepted.
External cleanup leaves files alone. Use the original workstation too if its
key files still exist. A fingerprint mismatch requires review, never a bypass.

After successful cleanup, the permitted journal change is key phase `removed`.
Keep mode, fingerprint and resource records unchanged. Do not mark resources
destroyed to make cleanup pass. Once the repair and release commit, rerun delete
to finish retirement.

If the original process crashed after committing key removal, the key is already
`removed` but its lock may still be held. Perform the same process, state and
provider checks above. Require deleting status and all records destroyed. Leave
the key unchanged and conditionally release the reviewed owner. The next delete
can then finish retirement. Do not rerun local cleanup under this shortcut.

## Commit the reviewed repair with a precondition

The following Python example uses the same transport as all three colours.
Run it in an environment with the Blue library and backend credentials. `opts`
and `environment` are the original deployment configuration. `observed` is the
private snapshot from the review above, and `repaired_key` is exactly the one
permitted change chosen above. Keep the original owner run ID in `reviewed_run`.
Do not set these values from a new unreviewed read.

```python
from copy import deepcopy
from uuid import uuid4
from colors_compute.journal import journal_get, journal_put
from colors_compute.lifecycle import lifecycle_document_valid

async def commit_reviewed_key_repair(opts, environment, observed,
                                     reviewed_run, repaired_key):
    def require(condition):
        if not condition:
            raise ValueError('reviewed recovery prerequisites do not match')

    require(observed.get('status') == 'present')
    original = observed['document']
    require(lifecycle_document_valid(original))
    require(original['lock'] == {'state': 'held', 'run_id': reviewed_run})
    require(original['revision'] < 9007199254740991)
    old_key = original['key']
    records = [original['shared'], *original['nodes'].values()]
    if old_key['phase'] == 'intent':
        require(original['status'] == 'active')
        require(all(r['phase'] in ('declared', 'destroyed') for r in records))
        reset = {'mode': None, 'phase': 'absent', 'fingerprint': None}
        require(repaired_key == reset or (
            repaired_key.get('phase') == 'prepared'
            and repaired_key.get('mode') == old_key['mode']))
    elif old_key['phase'] in ('cleanup', 'removed'):
        require(original['status'] == 'deleting')
        require(all(r['phase'] == 'destroyed' for r in records))
        require(repaired_key == {**old_key, 'phase': 'removed'})
    else:
        raise ValueError('recovery requires intent, cleanup or removed')
    require(await journal_get(opts, environment) == observed)
    updated = deepcopy(original)
    updated['key'] = deepcopy(repaired_key)
    updated['lock'] = {'state': 'idle', 'run_id': None}
    updated['revision'] += 1
    updated['write_id'] = uuid4().hex
    require(lifecycle_document_valid(updated))
    result = await journal_put(opts, {
        'condition': {'if_match': observed['etag']},
        'document': updated,
    }, environment)
    confirmed = await journal_get(opts, environment)
    if (confirmed.get('status') != 'present'
            or confirmed.get('document') != updated):
        raise RuntimeError('repair not confirmed; stop and review again')
    return result
```

For a reviewed held `removed` key, call the helper with the unchanged key:

```python
await commit_reviewed_key_repair(
    opts, environment, observed, reviewed_run, observed['document']['key'])
```

This helper performs the conditional write only. It cannot prove the process,
provider or local-file evidence required above, and must not run as a retry hook.
The transport sends If-Match on S3/R2/OCI and a generation precondition on GCS.
A conflict requires a new complete review. An ambiguous response permits only
read-back verification of the exact intended document; do not submit another
write or release a different owner. Never delete the journal, reset its revision,
change its generation, or issue an unconditional upload. Save the before/after
snapshots and the evidence in the incident record before restarting writers.
