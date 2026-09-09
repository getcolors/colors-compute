# Kubernetes cloud-controller artifact

`requirements.kubernetes_controller: true` requires a compatible provider
controller before any deployment operation. False, null and other values are
invalid when the field is present. The deployment request resolver validates the
controller's configured version using deterministic shared references, so missing
capability or invalid version fails before key creation or node fanout.

`controller_artifact(opts, shared_outputs)` returns a new object with:

- `filename`: `colors-compute-controller.yml`, an Ansible task include;
- `content`: provider-owned tasks with validated version and observed network ID;
- `credentials`: environment variable names, never their values;
- `namespace` and `rollout_resource`: the application's readiness target;
- `cluster_name`: the validated selected compute name or profile.

The application writes the content verbatim and imports the generic filename.
It may use the returned rollout target for acceptance. It does not select a
provider template, controller deployment name, credential name or manifest URL.
The artifact preserves literal Ansible environment lookups and `no_log` on
secret creation. Version and network substitutions reject shell/YAML injection;
operator credential values in opts are ignored and never rendered.

The initial descriptor supports DigitalOcean CCM, with the existing pinned
manifest download, in-memory Kubernetes Secret apply, VPC binding and rollout
wait. `kubernetes-controller-version` takes precedence over the historical
`digitalocean-cloud-controller-version`; either must be an exact
`vMAJOR.MINOR.PATCH`. The historical enabled setting, when present, must be true.
Other providers fail with an unsupported capability error until the library
ships their descriptor and task artifact.

Canonical metadata is `contracts/controllers.json`; provider task sources live
under `providers/<provider>/controller.ansible.yml.template`. Run
`scripts/controller_resources.py --write` to package the reviewed artifacts for
all three colors. Six shared fixtures execute against the native Blue, Red and
Green drivers. These checks cover rendering and refusals, not live CCM or cloud
load-balancer behavior. Kubernetes application/resource pruning remains the
application's responsibility before compute deletion.
