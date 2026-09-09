# Registration collision preflight

`registration_preflight(opts,key_mode,ownership=None,public_key=None,environment=None,http=None)`
returns checked or skipped, or raises a fixed credential-free error. It never
registers, imports, or deletes keys. External account references skip listing.
AWS relies on regional name uniqueness and shared success before node dispatch.
Other nonregistration providers skip this check.

Descriptors in registration-preflight.json define provider endpoint, token,
public material field, pagination method, page limit and scope category. Shared
ownership is exactly `{provider,scope,id}` from shared state's registration
output. DO and Vultr scopes are account; hcloud is project. These are scope
categories, not independent account identifiers: equality is confirmed against
the listing authenticated by the selected credentials. Credentials must still
select the deployment's original account/project; no cross-account adoption is
supported. Names or public material alone never establish ownership.

Blue checks every page before key generation. Missing owned ids, malformed
responses, duplicate ids, pagination loops, missing required pagination metadata,
transport errors, or more than1000pages/100000keys fail closed. A matching name
must have the owned id and scope. Matching unowned public material requires
explicit recovery after checking surviving hosts; foreign material must not be
deleted. Before first key generation, unavailable local material yields the
conservative foreign-registration diagnostic.

HTTP injection receives URL and headers and returns response bytes; production
requires status200, verified HTTPS, no redirects or ambient proxy use,30second
request timeout and2MiB response bound. UTF8 JSON is strict, BOM invalid. DO next
URLs must retain exact HTTPS origin/path and only page/per_page query fields.
Hetzner page numbers and URL-encoded Vultr cursors use fixed endpoint origins.
Tokens are never included in output or error text. No automatic retries.

The pagination contracts follow [DigitalOcean API documentation](https://docs.digitalocean.com/reference/api/reference/public-apis/),
[Hetzner API pagination](https://docs.hetzner.cloud/reference/hetzner), and
[Vultr's official SDK cursor example](https://github.com/vultr/govultr/blob/master/README.md).
