"""Pure contracts. No credentials, local SSH files, or remote state are read."""

from __future__ import annotations

import json
import os
import re
from importlib.resources import files


def registry() -> dict:
    # A fresh value prevents one consumer from changing another's provider set.
    return json.loads(files("colors_compute").joinpath("providers.json").read_text())


def _missing(value: object) -> bool:
    return value is None or isinstance(value, str) and (
        not value.strip() or value.strip().upper() == "REPLACE_ME"
    )


def _safe(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", value))


def _local_path(value):
    return (isinstance(value, str) and value.startswith('/') and '\\' not in value
            and '\x00' not in value and (value == '/' or
                 all(part not in ('', '.', '..') for part in value[1:].split('/'))))


def local_state_dir(opts: dict) -> str:
    if 'local-state-dir' in opts:
        path = opts['local-state-dir']
        if _missing(path):
            raise ValueError(':local-state-dir is required')
    else:
        home = os.environ.get('HOME')
        if not _local_path(home):
            raise ValueError(':local-state-dir must be an absolute normalized POSIX path')
        path = home.rstrip('/') + '/.local/state/colors'
    if not _local_path(path):
        raise ValueError(':local-state-dir must be an absolute normalized POSIX path')
    return path


def _selection(opts: dict, providers: dict) -> list[str]:
    return [
        f":provider-{slot} must be one of " + ", ".join(sorted(providers[slot]))
        for slot in ("compute", "backend")
        if not isinstance(opts.get(f"provider-{slot}"), str)
        or opts.get(f"provider-{slot}") not in providers[slot]
    ]


def validate(opts: dict) -> list[str]:
    providers = registry()
    errors = _selection(opts, providers)
    profile = opts.get("profile")
    if _missing(profile):
        errors.append(":profile is required")
    elif not _safe(profile):
        errors.append(":profile must be a safe identifier")
    if "compute-require-existing-state" in opts and type(opts["compute-require-existing-state"]) is not bool:
        errors.append(":compute-require-existing-state must be a boolean")
    required = set()
    for slot in ("compute", "backend"):
        selected = opts.get(f"provider-{slot}")
        entry = providers[slot].get(selected, {}) if isinstance(selected, str) else {}
        required.update(entry.get("required", []))
    errors.extend(f":{key} is required" for key in sorted(required) if _missing(opts.get(key)))
    for slot in ('compute', 'backend'):
        selected = opts.get('provider-' + slot)
        entry = providers[slot].get(selected, {}) if isinstance(selected, str) else {}
        for alternatives in entry.get('required-one-of', []):
            if not any(all(not _missing(opts.get(key)) for key in group) for group in alternatives):
                errors.append('one of ' + ' or '.join(' and '.join(':' + key for key in group) for group in alternatives) + ' is required')
    if opts.get('provider-backend') == 'local':
        try:
            local_state_dir(opts)
        except ValueError as error:
            errors.append(str(error))
    return errors


def credential_requirements(opts: dict) -> list[str]:
    providers = registry()
    errors = _selection(opts, providers)
    if errors:
        raise ValueError("; ".join(errors))
    return sorted({
        "COLORS_PAR_" + key.upper().replace("-", "_")
        for slot in ("compute", "backend")
        for key in providers[slot][opts[f"provider-{slot}"]]["secrets"]
    })


def compute_credential_errors(opts: dict, environment: dict) -> list[str]:
    providers = registry()
    selected = opts.get('provider-compute')
    if not isinstance(selected, str) or selected not in providers['compute']:
        raise ValueError('invalid compute provider')
    variables = sorted('COLORS_PAR_' + key.upper().replace('-', '_') for key in providers['compute'][selected]['secrets'])
    return ['required credential is not set: ' + variable for variable in variables
            if not isinstance(environment.get(variable), str) or _missing(environment.get(variable))]


def state_decision(read: dict, selected: str) -> dict:
    status = read.get("status")
    if status == "absent":
        return {"action": "create"}
    if status == "error":
        raise ValueError("could not read compute state; refusing mutation")
    if status != "present":
        raise ValueError("invalid state read status")
    recorded = (read.get("params") or {}).get("provider")
    if not isinstance(recorded, str) or _missing(recorded):
        raise ValueError("legacy state requires migration")
    if recorded != selected:
        raise ValueError(
            f"state holds a {recorded} machine; set provider-compute back to {recorded} and delete first"
        )
    return {"action": "reuse"}
