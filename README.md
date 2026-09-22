# Colors compute

Independent infrastructure capabilities for Green, Red, and Blue:

- Durable encrypted SSH resources with explicit stable names.
- Provider public-key registrations, each owned once and shared explicitly.
- Compute nodes consuming public identity only.
- Temporary SSH agents owned by the caller's workflow scope.

The Colors SDK owns topology, fan-out, joins, and cleanup ordering. Create a
stable SSH resource, create any required registration, then feed its public
identity to one or more nodes. Application convergence also waits for the scoped
agent. Neither compute nor registration state contains an SSH private key.

See [compute and registration API](contracts/node.md),
[SSH resources](contracts/ssh-resource.md), and
[local compute state](contracts/local-backend.md).
This is a [greenfield breaking replacement](migration/README.md).

## Providers and storage

Compute templates cover AWS, Azure, DigitalOcean, Google, Hetzner Cloud, OCI,
Vultr, and Yandex. AWS, DigitalOcean, Hetzner Cloud and Vultr require separate
public-key registrations. Other providers receive the public key directly.
Compute owns the machine and exclusively owned supporting infrastructure.

Compute state supports S3, Cloudflare R2, OCI, GCS, and local storage.
Each node runs in `<workdir>/<profile>/<node_id>` with persistent OpenTofu
configuration. Remote state uses `<s3-prefix>/<profile>/<state_filename>`.
SSH resource storage and locking are described in their own contract.

## Checks

```sh
python3 scripts/registry.py
python3 scripts/provider_resources.py
python3 scripts/provider_recipes.py
python3 scripts/ssh_resources.py
uv run --project blue python scripts/parity.py
uv run --project blue python scripts/ssh_parity.py
uv run --project blue python scripts/ssh_lifecycle.py
uv run --directory blue pytest -q
(cd green && bb test)
(cd red && bun install --frozen-lockfile && bun test && bun run typecheck)
```

Shared fixtures compare complete three-color node and registration plans.
Lifecycle tests exercise public identity binding, independent registration
ownership, persistent workdirs, replacement/deletion guards, and diagnostics.
Provider schema validation and synthetic runners do not establish cloud
permissions or application readiness; live verification is separately recorded.

## Explicit live protocol probe

`scripts/ssh_remote_probe.py --bucket alice-state --endpoint <EU-R2-endpoint>
--profile alice-digitalocean` uses runtime R2 credentials to test concurrent
creation, rotation, fresh-workstation access, and deletion. It writes one
profile-namespaced resource and retains its deletion tombstone; it creates no
machine. This is an explicitly invoked live check, not part of the offline suite.
