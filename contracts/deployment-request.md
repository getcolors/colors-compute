# Application requests and planning

`deployment_requests(opts, topology, requirements, key)` returns shared and node
requests. Requirements contain security and optional network, single_host,
private, and legacy_state_keys. Network mode defaults to the provider recipe.
Packages supply application needs and never select a mode by provider name.
Private=true requires private addresses in the final join. Legacy state keys
identify earlier monolithic states that must be migrated before a fresh run.

single_host defaults false and must be explicit for an unnumbered machine.
It permits only one node with a null role. Cluster names always retain node IDs,
including when there is one node, so scale-up cannot rename node zero. The
provider-scoped name override defaults to profile. Unsafe or overlong derived
names fail instead of being truncated. Profile-based SSH identity is separate.

`key_request(opts, prepared, environment)` resolves the recipe's external key
kind. Account IDs remain IDs. Content must be an OpenSSH public key. File mode
reads one explicitly supplied regular .pub file, expanding ~/ using the supplied
HOME. Symlinks and multiple public files are refused. No private file is opened.
Managed mode passes the generated public key. Build and dry-run never open
external key files and use a deterministic public placeholder instead.
External identity paths use ssh-private-key-path, with the selected provider's
legacy `<provider>-ssh-private-key` setting as a fallback. Neither is read.

`plan_deployment(opts, topology, requirements)` returns status=planned,
documents={shared,nodes}, state_keys, cluster, shared outputs and key references. It reads only
packaged data. It never reads environment variables, credentials, remote state
or local SSH files, and never runs a process. Recipes carry non-secret planning
shared outputs. Public addresses start at 192.0.2.10 and private addresses use
the supplied subnet plus the same offset, falling back to 10.0.0.0/24 for
discovered networks. It refuses address exhaustion. Planning documents and
addresses are never used as evidence by real orchestration.

Shared params include network_cidr across providers. Real runs report the
provider's observed network range; planning uses the configured range or its
discovery fixture. Runtime and inspection also attach ssh_identity_file to each
node when a local identity path is known.
