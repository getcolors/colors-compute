# SSH resource implementation handoff

Completed 2026-09-22. This is the final implementation record; the earlier
`SSH-RESOURCE-HANDOFF.md` is historical design input.

## Outcome

Implemented the greenfield v2 design in Green, Red and Blue, integrated Alice,
pushed all changes to main, and verified Alice live on DigitalOcean with local
storage first and then Cloudflare R2 EU. No legacy state migration was attempted.

Named SSH resources own encrypted ED25519 keys independently of compute and
provider public-key registrations. Compute receives only public identity and
pins its reference/fingerprint. One scoped agent unlocks explicitly selected
identities; process cleanup precedes agent cleanup. No operator agent mutation,
agent forwarding, or decrypted private-key file is required. Compute deletion
retains encrypted SSH authority and works without the SSH passphrase.

The normative API and recovery details are in `contracts/ssh-resource.md`.
The Python process adapter is packaged identically in all three native libraries;
`scripts/ssh_resources.py` verifies the copies. Encryption uses OpenSSH
bcrypt with 64 rounds. Generation and rotation use controlled, echo-disabled
PTY prompts; agent loading uses a restricted single-attempt askpass process.

Local authority uses an OS lock. Remote authority is one conditionally updated
object, with explicit reservations and recovery tokens. Failed reads never prove
absence. Tombstones prevent resource-name reuse. Public selector files are
`identity-<SHA256-of-reference>.pub`, separated by full backend identity.

## Published commits

| Repository | Main implementation/stamp commit |
| --- | --- |
| green | `7f1f94463ac7914598db419ac221546fb7e3cbec` |
| red | `f28d4398cda1a2f6ca074011b924ed0e9da0ce4a` |
| blue | `784018bb586882efbd91affd42139582a40ea1d1` |
| colors-compute | `2f0e95fb3ed7acc438718b1b8ead119630e0d839` |
| alice implementation and dependency pins | `988b96dbe689f9c0850cc9ab1fbeb6481d7c1a7b` |
| alice stamped launcher | `77d16b765448cd9e4bbeb0f58df1aa293bfa3326` |
| alice-digitalocean | `131a5e4f74c6072abc3e4004eb0e0ebb15dc98fa` |
| workspace standards | `cb2bd48601c25810b2686b1e10838c34a7777526` |

All were clean and matched origin/main before this final documentation commit.
Alice pins the published SDK and compute commits above. Its installed root
launcher and Package Skill launcher are byte-identical. The deployment retains
its manual-copy provenance; no installation lockfile was fabricated.
This handoff is a final documentation-only commit after the implementation pin.

## Verification

- SDK: Green Babashka and JVM each 147 tests / 580 assertions; Red 143 passed,
  one opt-in skip, plus typecheck; Blue 156 passed, one skip.
- Compute: Blue 183 passed; Green 32 tests / 622 assertions; Red 79 passed /
  589 assertions plus typecheck.
- Compute/registration parity: 262 cases per color. Native SSH plan parity:
  35 cases per color. Adapter, registry, provider template and recipe copy checks
  passed. Real cross-color lifecycle created in Green, reused and loaded in all
  three colors, rotated in Red, and deleted in Blue.
- Alice: 62 tests / 204 assertions, golden fixtures, seven launcher checks.
  A copied published launcher built successfully with local overrides unset.
- ONCE downstream parity passed against all three updated SDK working trees,
  including generated artifacts, DAG ordering and 53 DMARC validation cases;
  ONCE source and dependency pins were unchanged.
- Real OpenSSH tests ran on macOS and the Ubuntu test Droplet: encrypted key
  generation, identity-preserving rotation, two independent keys, scoped cleanup.
- R2 protocol probe tested concurrent creation, one stable identity, rotation,
  retrieval from a fresh workdir, agent cleanup and deletion to a tombstone.

Local Alice live validation included create, tunneled web-UI acceptance,
reachable/active describe, repeat create, credential-free preview build,
public-only state inspection, Blue access to Green-created authority, and delete
without a passphrase. Its runtime directory is `.colors/ssh-resource-local`.

R2 validation included first create and tunneled UI acceptance, then repeat
create through the published installed launcher with all library overrides
unset. Describe reported reachable SSH and active Transmission. Credential-free
build and dry-run passed. A fresh resolved temporary workdir retrieved the R2
identity through Blue, authenticated to the actual server, checked Transmission,
confirmed no agent forwarding, and removed its scoped agent. Remote compute and
registration state had no TLS private-key resource, private-key PEM, passphrase,
DigitalOcean token or R2 secret. Delete through the published launcher succeeded
with the SSH passphrase unset.

The live runs exercised create/acceptance/describe/delete. The full torrent
`sync` download and final rsync lifecycle was not run during this implementation;
its changed scope and tunnel handling is covered by package tests.

## Final deployment and retained state

Deployment: `/Users/amiorin/code/getcolors/alice-digitalocean`.
Profile: `alice-digitalocean`. Backend: `r2`. Bucket: `alice-state`.
EU endpoint:
`https://319271fed8bc6d2d9059362be1165f37.eu.r2.cloudflarestorage.com`.
`s3-prefix` is empty, so every object key contains the profile:

- `alice-digitalocean/alice-node-0.tfstate`: empty after destroy.
- `alice-digitalocean/alice-ssh-registration.tfstate`: empty after destroy.
- `alice-digitalocean/ssh/app-access/resource.json`: ready, encrypted authority
  retained for subsequent creation and explicit recovery/rotation.
- `alice-digitalocean/ssh/protocol-probe-4b7d20df4917/resource.json`:
  disposable probe tombstone, no ciphertext.

Final DigitalOcean API checks found no Alice Droplets or SSH registrations.
R2 had no OpenTofu lock objects. Both live test machines were removed.
The R2 bucket and durable encrypted identities intentionally remain.
Historical `.colors` records and local encrypted authorities were preserved;
never discard them as an incidental cleanup operation.

Current runtime workdir is `.colors/ssh-resource-r2`; preview builds are isolated
under `.colors/ssh-resource-r2/build/<profile>` and cannot replace live templates.
`compute-prevent-destroy: true` remains committed. Alice's explicit delete, or
successful final sync copy, is the destruction authorization boundary.

Ignored `.envrc.private` is mode 0600 and contains the existing DigitalOcean
token, generated SSH-resource passphrase, and user-supplied R2 credentials:
`COLORS_PAR_DO_TOKEN`, `COLORS_PAR_ALICE_SSH_PASSPHRASE`,
`COLORS_PAR_R2_ACCESS_KEY_ID`, `COLORS_PAR_R2_SECRET_ACCESS_KEY`.
No secret values are in this handoff or committed files. Never export
`COLORS_PAR_PROFILE`. Changing the passphrase value is not a rotation operation.

With the deployment environment loaded, use the installed `./green build`,
`./green create --dry-run`, `./green create`, `./green describe`,
`./green tunnel 19091`, and explicit `./green delete`. Create provisions a new
Droplet using the retained R2 SSH identity. Runtime tools include Python with
POSIX PTY support, OpenSSH, AWS CLI, OpenTofu, Ansible, curl and rsync.

## Recovery and limits

Locks are not leases and are never automatically stolen. Recovery requires the
exact lock token, stopped competing writers, and a verifiable existing encrypted
bundle. An interrupted first reservation with no persisted bundle requires an
explicit decision to abandon the resource name; recovery never generates a
replacement identity. Preserve backend history and supply expected identity when
retrieving a known resource from another workstation.

Agent identity lifetime defaults to 900 seconds and renews only while its caller
pipe remains open. SIGKILL cannot execute cleanup; bounded lifetime limits the
remaining authority of an orphaned agent. Rotation can require active scopes to
restart with the new passphrase binding before their next renewal.

Other compute providers and S3/OCI/GCS backends have plan/synthetic coverage,
not live end-to-end verification from this run. Remote storage requires atomic
conditional writes; there is no unconditional fallback. Older downstream
packages remain on their existing pins until explicitly integrated with v2.
