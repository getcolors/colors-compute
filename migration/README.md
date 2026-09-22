# Greenfield v2 boundary

The separated SSH-resource, compute, registration, and agent API is a greenfield
breaking replacement. It intentionally supplies no compatibility layer, private
key adoption, state transfer, or migration tooling. Use fresh identities and
state roots; changing a pin does not transfer ownership of existing resources.

Historical scripts in this directory are read-only inventory tools for older
layouts, not a supported migration into v2. Do not use their proposed mappings to
apply the new API to old state. Existing resources remain owned by their original
configuration until explicitly destroyed or separately reconciled by an operator.
