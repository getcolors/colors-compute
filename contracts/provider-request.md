# Provider-neutral request resolution

`provider_request(opts, stage, request, shared={})` is pure. It selects the
packaged recipe for opts.provider-compute, resolves typed input bindings and
returns `{provider, stage, inputs, documents}`. `stage` is `shared` or `node`;
returned stage may be shared-keygen or an OCI image variant. Shared results use
flattened OpenTofu output values: `{params:{...},ssh_key_id:...,key_name:...}`.
No credentials, SSH files, cloud discovery or state operations are performed.

Request fields are exactly node_id, name, key, network, security. node_id must
be safe; name is a safe default display name (optional: profile on shared,
profile-node_id on node). key has mode managed/external, public_key (needed for
managed registrations and content-based providers), optional ids (external
registration reference list), optional reference (primary external key reference).
network has mode created/discovered, optional cidr, subnet_cidr, zone, private_ip.
security has ingress (nonempty list), egress exactly all, private_filter boolean.
Each ingress rule has exactly id, protocol tcp/udp/icmp, from_port, to_port, sources.
Rule IDs are safe and unique. TCP/UDP ports are ordered integers in 1..65535.
ICMP requires both port fields to be null and permits all types and codes. Sources are
nonempty unique canonical IPv4 CIDRs or literal private. At least one public TCP
rule must include port22. Reserved private resolves to network CIDR, except DO
where the provider resolves the discovered default VPC CIDR. Rule expansion
uses `<rule-id>:<source>` stable IDs, not list indices. Duplicate source entries,
unknown fields, unsupported network modes and private-filter requirements fail.
For hcloud private_filter must be false and no private-source rule may be supplied:
provider firewalls cannot enforce private filtering. OCI private_filter true is
unsupported because existing subnet security lists may grant broader access.
All other formats support the requested provider private-source rule behavior.

Settings live only in recipes, never consumers. Options retain existing
provider-scoped names; bound missing settings fail before rendering. Request
CIDRs describe topology independently of the provider. Library bindings may
fall back to existing provider CIDR settings during initial package migration.
No network mode none, IPv6, restrictive egress, role-specific firewall or
reserved-IP capability is silently approximated in this initial resolver.

Canonical data is contracts/provider-recipes.json, packaged per color as
provider-recipes.json. Recipes define network_mode, private_filter, registration,
firewall_format and input bindings. A binding is `{path:"opts.key"}`,
`{path:"request.network.zone"}`, `{path:"shared.params.vpc_id"}` or
`{path:"derived.name"}`, with optional default; `{first:[bindings...]}` selects
the first nonmissing result; `{list:[bindings...]}` and `{object:{...}}` build
containers; `{value:...}` is literal. Missing means absent/null/blank/REPLACE_ME;
a default explicitly permits null. Requested fields never become arbitrary
OpenTofu JSON overrides. Bind only tokens used by the selected packaged stage.

Derived values include profile/name/node_id, protect flag (opts
compute-prevent-destroy defaults true), registry user/sudoer, resolved key IDs,
network CIDRs/address/prefix, generated resource names, image-reference fields
and format-specific firewall inputs. Details are executable in the Blue
reference resolver and parity fixtures added with the ports. Mutating returned
inputs/documents must not mutate opts, request, shared or packaged recipes.

OCI node chooses node when oci-image-id is supplied, otherwise
node-discovery. Yandex selects node with yandex-image-id or node-discovery using
yandex-image-family; family discovery preserves the boot image through
ignore_changes, matching its existing package behavior. Google supports explicit google-image-id or
project/family derivation. Provider capability failures and missing binding
messages are library errors; they contain field names, never supplied values.

Resolved input strings must not contain Terraform interpolation `${` or directive
`%{` openers; reject with `invalid compute literal` before provider rendering.
Provider-owned Terraform expressions stay in templates and are never input data.
Private IPs must be strings. Numeric key references must be integral numbers
from1through9007199254740991 (booleans excluded); strings must be nonmissing.

Recipes declare static_private_ip capability. A nonmissing private_ip is
rejected with `unsupported compute static private address` when false; only
hcloud currently supports that input. No accepted private address is ignored.

Recipes also declare external_key_kind as ids, content, or public_file.
An external public file remains operator-owned. AWS registers its public
content in the deployment's shared state, as its existing setting requires;
registration_external=true records that distinction. Supplied account IDs
create no registration. Both managed and external AWS registrations therefore
come from shared outputs, never from interpreting a local path as a key name.
