# Colors compute contracts

[Single compute unit](node.md) is the normative lifecycle and ownership contract.
The public API contains `node_plan`, `build_node`, and `compute_node`, plus the
pure provider selection, rendering, and credential helpers. Green uses its native
kebab-case names.

[Provider request rendering](provider-request.md) and [template/backend rendering](rendering.md)
describe the internal provider fragments used to compose each single-node root.
Shared/node fragment names are implementation details: they do not create
separate states or authorize deployment orchestration.

The canonical registries are `providers.json`, `provider-recipes.json`, and
`compute-options.json`, copied into each language distribution by checked scripts.
Tests compare complete node plans across Green, Red, and Blue.

The previous coordination, journal, topology, and managed-backend APIs are
removed. Existing consumers must stay pinned until explicitly migrated.
See [migration](../migration/README.md). No source update transfers live state.
