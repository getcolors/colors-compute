# Executable contract, version 1

This is the initial library foundation. These pure operations MUST have the
same JSON behavior in Green, Red, and Blue. Lifecycle execution is a separate
layer and must not be claimed complete by implementing these functions alone.

Inputs are JSON-compatible maps. Desired-state keys are kebab-case. Results
use the exact keys below, independent of implementation language. Errors are
arrays of strings or exceptions with the same message for invalid pure calls.

## Operations

- `validate(opts)` returns an ordered array of errors. Validate selection first:
  `:provider-compute must be one of ` plus sorted compute names, then
  `:provider-backend must be one of r2, s3`. Missing profile is
  `:profile is required`; invalid profile is `:profile must be a safe identifier`.
  Safe identifiers match `[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}`. Check sorted required
  keys from selected compute and backend entries, with `:<key> is required`.
  Missing means null, absent, blank string, or case-insensitive REPLACE_ME.
  Name overrides are optional. Credentials are not validated by this operation.
- `credential_requirements(opts)` returns sorted unique COLORS_PAR names from
  selected compute and backend secrets. Throws selection errors joined by `; `
  for an invalid selection. Ambient credentials require no COLORS_PAR names.
- `state_keys(profile, node_ids)` returns `{"shared": "<profile>/compute/shared.tfstate",
  "nodes": {"<id>": "<profile>/compute/nodes/<id>.tfstate"}}`. All ids use the
  safe identifier grammar. Duplicate ids throw `duplicate node_id: <id>`.
  Invalid ids throw `invalid node_id: <id>`; invalid profiles throw
  `:profile must be a safe identifier`.
- `expand(topology)` accepts a list of role declarations with `role` null or
  `[a-z][a-z0-9]*(-[a-z0-9]+)*` and `count` positive integer, default 1.
  Returns ordered `[{"node_id": "0" or "role-0", "role": null or string,
  "index": 0}, ...]`. No bool counts. Empty topology throws
  `topology must declare at least one role`. Invalid role throws `invalid role`;
  duplicate role throws `duplicate role`; a null role with other declarations
  throws `a null role must be the only role`; invalid count throws
  `count must be a positive integer`. Generated ids must pass state_keys checks.
- `collect(requests, results, entry_node_id)` returns `{"provider": provider,
  "entry_node_id": id, "nodes": [...]}`. Validate nonempty requests and unique
  ids first; entry must name one request. Error messages: `no nodes requested`,
  `duplicate requested node: <id>`, `unknown entry node: <id>`,
  `undeclared node: <id>`, `duplicate node: <id>`, `missing node: <id>`,
  `incomplete node <id>: <field>`, `provider mismatch: <id>`.
  Examine results in arrival order for undeclared/duplicate ids, then requests
  in declared order for missing/invalid results. Required fields, in order:
  provider, name, ip, user, sudoer. Require vpc_ip if request `private` is true.
  These fields must be nonblank strings. Preserve every result field, but set
  role and index from the request. Compare providers with request.provider when
  given, otherwise with the first result in request order. Return nodes in
  request order. This operation never uses placeholders.
- `state_decision(read, selected)` consumes a tagged result `status` of absent,
  present, or error. Absent returns `{"action":"create"}`. Error throws
  `could not read compute state; refusing mutation`. Present requires
  `params.provider`; absent provider throws `legacy state requires migration`.
  A mismatch throws `state holds a <recorded> machine; set provider-compute back
  to <recorded> and delete first`. Match returns `{"action":"reuse"}`.
  Unknown status throws `invalid state read status`.

Selection and error ordering are part of parity. Tests must cover malformed
inputs, partial results, arrival-order changes, metadata preservation, and
scaling without identity changes. JSON fixture drivers accept one JSON object
per line: `{"op": name, "args": [...]}`. They print one JSON result per line,
or `{"error": "message"}` when an operation throws. No progress output on stdout.

The provider registry in `providers.json` is canonical. Package resources may
contain generated copies for distribution; a check must reject drift.
