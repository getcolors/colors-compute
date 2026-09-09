# Application-assigned endpoints

An application may add `endpoint: {kind: "reserved-ip", assignment: "application"}`
to its deployment requirements. Assembly passes this strict object to shared and
node requests. Unknown fields, null, other kinds, and other assignment policies
are errors. A recipe must declare `application_reserved_ip: true`; otherwise the
request fails with `unsupported compute endpoint capability` before key creation.
Currently only DigitalOcean supports this capability.

The shared DigitalOcean stack reserves a region-scoped address. It does not set
`droplet_id` or create an assignment resource. It ignores subsequent changes to
`droplet_id`, because primary election belongs to the application. The resource
uses the deployment's `prevent_destroy` guard. Its count is zero without the
capability request and one with it. `shared.params.endpoint_ip` is the allocated
address (null when no endpoint was requested).

All node outputs include `provider_id`, the provider's actual resource identifier.
This is distinct from `node_id` (stable topology identity) and `uid` (existing Unix
login metadata). Planning returns deterministic placeholder provider IDs and
`198.51.100.10` for a requested endpoint; these are never runtime observations.

`endpoint_agent(provider)` / `endpoint-agent` returns a detached object:
`{filename: "colors-compute-endpoint", content: <standalone Python3 source>,
credentials: [<required environment variable names>]}`. It reads only packaged
resources. Applications install the artifact and supply its declared credentials
through their protected secret environment. No provider API implementation or
credential mapping belongs in the application.

The agent accepts `--provider`, `--ip`, `--node-id`, and optional
`--action assign|status` (default assign). `status` does not require a node ID and
returns `{status:"present",node_id:<provider ID string or null>}`. Assignment
returns `{status:"assigned",node_id:<provider ID string>}` only after confirming
the current holder. All CLI failures return `{status:"error"}` and exit one;
credentials and API responses are never printed. A valid IPv4 address and a
positive safe integer DigitalOcean ID are required.

The caller must establish primary ownership and write eligibility before invoking
assignment. The agent is not an election mechanism, lock, or fencing service. It
reads the existing endpoint, returns if already assigned, otherwise unassigns and
confirms before assigning and confirming the new holder. Failed reads, malformed
responses, failed actions, and timeouts refuse further work. It never creates or
deletes an endpoint. There is no blind retry of a failed mutation. HTTPS requests
have no redirects or ambient proxy handling, a 2 MiB response limit, and a shared
120-second operation deadline. No command contains a credential argument.

This preserves the provider boundary while applications retain their database
health and election logic. New provider capability requires a library adapter,
recipe, and validated template; applications consume a version bump.

The DigitalOcean resource and action behavior follow its official
[reserved IP resource](https://registry.terraform.io/providers/digitalocean/digitalocean/latest/docs/resources/reserved_ip)
and [assignment API](https://docs.digitalocean.com/products/networking/reserved-ips/how-to/modify/).
Tests use synthetic transports and backend-disabled OpenTofu validation. No live
endpoint assignment or provider deployment is claimed.
