# Minimal coordination transition contract

Status: executable pure-contract proposal, 2026-09-09. This is a deliberately
limited first slice of `coordination-design.md`, not a complete lifecycle or
transport protocol. Function `coordination(observation, identity, event)` plans
one conditional object write. It performs no I/O and grants no permission to
dispatch provider work until the coordinator confirms that exact write committed.
Language-specific public naming may follow existing conventions.

## Strict schemas

All maps below reject unknown fields. Required fields must be present, even when
nullable. Integer constraints are mathematical JSON integers: 1.0 is accepted as 1;
booleans are never integers. A safe ID
matches `[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}`. A nonblank string has non-whitespace
content. Comparisons are exact, case-sensitive, and structural for objects.
Object key order does not affect equality. Input documents must not be mutated.

`identity` has exactly `profile`, `provider`, `backend`. Profile is a safe ID.
Provider is any key in the packaged compute registry, not a duplicated list.
Backend has required `kind`, `bucket`, `region`; kind is `s3` or `r2`. Bucket
matches `[a-z0-9][a-z0-9.-]{0,62}`. Region is a safe ID. S3 forbids any `endpoint`
field. R2 requires `region: "auto"` and `endpoint` matching
`https://[a-zA-Z0-9.-]+(:[0-9]{1,5})?/?`, excluding credentials/query/path. These
checks are structural; full provider/bucket/endpoint semantics remain runtime
validation. Backend changes never create permission to abandon old resources.

Observation is one of exactly:

- `{"status":"absent"}`: independently confirmed coordination-object absence.
- `{"status":"error"}`: no reliable observation.
- `{"status":"present","etag":"opaque nonblank string","document":...}`.

A journal document has exactly these fields:

```json
{
  "schema_version": 1,
  "identity": {"profile":"demo","provider":"vultr","backend":{"kind":"s3","bucket":"states","region":"eu-west-1"}},
  "revision": 1,
  "write_id": "write-1",
  "lock": {"state":"held","run_id":"run-1"},
  "topology_declared": false,
  "nodes": {}
}
```

Revision is an integer from 1 through 9007199254740991; schema_version is integer
1. write_id is a safe ID. Lock fields are exactly state and run_id; state is
held with safe run_id, or idle with null run_id. topology_declared is boolean.
Nodes is a map of at most 1000 safe node IDs to records with exactly:

```json
{"state_key":"demo/compute/nodes/broker-0.tfstate","role":"broker","index":0,"phase":"declared","operation_id":null}
```

Role is null or matches `[a-z][a-z0-9]*(-[a-z0-9]+)*`. Index is a nonnegative
integer no greater than 999. Node ID must equal index as decimal for null role,
or `<role>-<index>`. Each role's indices must be contiguous starting at zero.
Null role cannot coexist with named roles. State keys must match `state_keys`
for document.identity.profile exactly; arbitrary caller-supplied paths are
forbidden. Phase is declared/running/ready/failed. Declared requires null
operation_id; every other phase requires a safe operation_id, unique among all
node records. Running requires a held lock. False topology_declared requires
empty nodes; true requires nonempty nodes. No timestamps, owner labels, raw
state, result metadata, errors, credentials or arbitrary extension fields exist.

Every event requires `type`, safe `run_id`, safe `write_id`, and `target_etag`
(null or nonblank string). Types and additional required fields are:

| Type | Additional fields |
|---|---|
| acquire | none |
| declare | topology |
| start | node_id, operation_id |
| complete | node_id, operation_id |
| fail | node_id, operation_id |
| release | none |

Node/operation IDs in events must be safe IDs. Topology is a nonempty array of
strict role declarations containing only optional role and count fields. Role
omission means null; count omission means 1. Roles must satisfy the grammar
above and be unique, with null allowed only alone. Count is a positive integer;
the total is at most 1000. Expansion follows existing `expand` ordering and IDs;
all expanded IDs must pass safe-ID validation. Invalid topology is an invalid
event, not an independently exposed expand error.

## Error ordering and messages

Throw the first applicable error, without echoing supplied values:

1. Invalid identity: `invalid coordination identity`.
2. Invalid event shape/fields/topology: `invalid coordination event`.
3. Invalid observation shape/tag/ETag: `invalid coordination observation`.
4. Error observation: `coordination read failed`.
5. Present but invalid journal (including nested identity):
   `invalid coordination document`.
6. Present journal identity differs from expected: `coordination identity mismatch`.
7. Event target_etag differs from observed ETag (or is non-null on absence):
   `stale coordination observation`.
8. Present event write_id equals journal write_id: `coordination write_id reused`.
9. Present revision is already maximum safe integer: `coordination revision exhausted`.
10. Transition rules below, in their stated order.

Freshness check only compares the current write_id. The coordinator must supply
cryptographically random globally unique run/write/operation IDs; the reducer
cannot prove historical uniqueness without an unbounded history. Revision plus
fresh write_id prevents identical-body ABA writes when the protocol is followed.
ETags remain opaque: never derive them from revision, write_id or document bytes.

## Transitions

On absent observation, only acquire is valid; others throw
`coordination object absent`. Acquire creates revision 1, a held lock for the
new run, topology_declared false, and empty nodes. Its condition is
`{"if_none_match":"*"}`.

On present observation, acquire first refuses held locks with
`coordination lock held`, even if run_id matches. An idle journal is acquired
without changing topology or node outcomes. Reattaching to a running owner,
lease expiry and stale-lock takeover are never inferred.

All other present transitions require a held lock, otherwise
`coordination lock not held`, then matching run_id, otherwise
`coordination owner mismatch`.

- declare: refuse an already-declared topology with
  `coordination topology already declared`. Atomically record the entire
  expanded topology, derived state keys, phase declared and null operation_id
  for every node; set topology_declared true. Topology is immutable in this
  slice, including across release/reacquire.
- start: require topology_declared, else `coordination topology not declared`;
  require node membership, else `coordination node undeclared`; require declared
  phase, else `coordination node not startable`; require operation_id unused by
  every node, else `coordination operation_id reused`. Set running and record
  operation_id. Only after this CAS commits may the coordinator dispatch this
  node. The complete intended node set was durably declared earlier.
- complete/fail: require topology_declared, then membership with the same errors;
  require running phase, else `coordination node not running`; require matching
  operation_id, else `coordination operation mismatch`. Set ready or failed,
  preserving operation_id and every sibling record. Caller must establish that
  the attempt terminated before issuing either event. A failure records an
  uncertain resource outcome, not proof of resource absence or cleanup.
- release: refuse any running node with `coordination operations outstanding`;
  otherwise set lock to idle/null and retain all records. Declared-but-unstarted
  and failed nodes are retained and do not represent a running child in this
  model. Release requires runtime proof no child/API operation remains active;
  journal inspection alone does not supply that proof.

Every successful present transition increments revision and sets event.write_id.
Output is exactly `{"condition":{"if_match":observed_etag},"document":new_doc}`
(or the if_none_match condition on first acquire). No authorized-actions list is
returned. A failed CAS invalidates the intention; never dispatch, blindly retry
an action, drop a node outcome, or unconditionally overwrite the object.

## Explicit limits and handoff

No transport, conditional-write execution, CAS-conflict retry, lost-response
readback, process ownership evidence, shared-key phases, per-node state locks,
provider reconciliation, retries of failed nodes, scale-down, deletion, retired
tombstones, history compaction or recovery takeover is implemented by this
slice. Those need separately specified transitions and runtime evidence before
package lifecycle adoption. A ready node can have application work pending;
this journal does not attest application readiness.

`test/fixtures/coordination.json` contains common pure cases in the existing
name/op/args/expected fixture format. Implementations must also test immutability
and strict rejection of unreviewed fields. A fake conditional object transport
must later test two contenders, stale writes and ambiguous responses; reducer
fixtures alone cannot demonstrate distributed-lock correctness.
