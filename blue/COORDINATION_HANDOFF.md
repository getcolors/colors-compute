# Blue coordination reducer handoff

Implemented 2026-09-09: pure `coordination(observation, identity, event)` in
`src/colors_compute/coordination.py`, exported publicly and exposed through the
shared JSONL contract driver. It produces conditional-write intentions only;
there is no network transport, write execution or provider dispatch authority.

Strict schemas reject unknown fields at every declared boundary, including
nested identity, backend, lock and node records. The reducer checks ordered
validation/freshness/ownership errors, retains sibling outcomes, deep-copies
returned documents, and normalizes mathematically integral JSON counts before
topology expansion. Huge Python integers are bounded before numeric conversion,
so malformed inputs retain fixed non-secret error messages.

Validation: all common coordination fixtures and Blue immutability/strict-field
regressions passed. Targeted run: 62 tests. Complete Blue suite: 130 tests passed.
The full run includes existing read-only runtime, rendering and workflow tests.
No live calls or package lifecycle changes ran.

Next work belongs to the separately reviewed coordination transport/coordinator
protocol: confirm conditional writes before dispatch, maintain process ownership,
handle ambiguous responses, and add explicitly specified retries/deletion/key
phases. The reducer deliberately cannot supply those runtime guarantees.
