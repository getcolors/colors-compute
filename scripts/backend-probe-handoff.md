# Backend probe handoff

2026-09-09. Added `backend_probe.py`, which runs only a local backend in a
temporary directory. OpenTofu 1.12.5 returns exit zero and empty stdout/stderr
for absent local state. A synthetic valid v4 state roundtrips with expected
serial, lineage, resources and outputs. Temporary files are removed.

Independent Blue review initially reproduced two defects: killing only the
parent left inherited pipes held by descendants (150ms timeout took 1.053s),
and JSON escaping allowed a backend secret containing a quote to return in
params. The Blue owner fixed both concurrently. The final optional
`--blue-runner` probe passed with 153ms elapsed and escaped-secret rejection.
The probe uses fixed, bounded one-second child processes and fake credentials;
it does not read real credentials or use any remote backend.

```sh
python3 scripts/backend_probe.py --tofu /absolute/path/to/tofu --blue-runner
```

This evidence validates local read classification and regression cases. It does
not prove S3/R2 absent-state behavior, coordination, cloud cancellation or
process-tree behavior on non-POSIX systems. All three color suites now include descendant timeout and credential-escaping
checks. The separate backend_http_probe.py also exercises all native readers
against a loopback-only S3 endpoint. No Blue source files
were changed by this review task.
