# Optional VM policies

Deployment requirements and provider requests accept `ipv6` (boolean) and
`backups`. Omission leaves the reviewed template unchanged. An explicitly
requested option requires a provider adapter; unsupported selections fail
before key generation or resource execution.

Backups accepts exactly `{enabled:false}` or
`{enabled:true,schedule:{type:"daily",hour:0..23}}`. Hour is a mathematical integer;
booleans, extra fields and other schedule kinds are refused. Vultr supports both
options: `enable_ipv6`, `backups:"enabled"|"disabled"`, and a daily
`backups_schedule` block. The provider defines the schedule timezone. Package
GitHub DWH preserves its existing daily hour 3 and disabled IPv6 settings.

`compute-options.json` owns provider resource/field mappings. Each distribution
packages that descriptor. New provider support belongs in the library, with
schema validation, rather than adding a package-side provider branch. Optional
fields are added only to the descriptor-named node resource; shared output is
unchanged. Managed Kubernetes does not use these VM requirements.

OpenTofu 1.12.5 init (`-backend=false`) and validate passed the enabled daily
backup/disabled IPv6 example using Vultr provider 2.32.0. This is a provider
schema check, not evidence of a live deployment.
