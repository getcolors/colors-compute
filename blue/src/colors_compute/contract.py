"""Pure contracts. No credentials, local SSH files, or remote state are read."""

from __future__ import annotations

import json
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
    required = set()
    for slot in ("compute", "backend"):
        selected = opts.get(f"provider-{slot}")
        entry = providers[slot].get(selected, {}) if isinstance(selected, str) else {}
        required.update(entry.get("required", []))
    errors.extend(f":{key} is required" for key in sorted(required) if _missing(opts.get(key)))
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


def state_keys(profile: str, node_ids: list[str]) -> dict:
    if not _safe(profile):
        raise ValueError(":profile must be a safe identifier")
    nodes = {}
    for node_id in node_ids:
        if not _safe(node_id):
            label = node_id if isinstance(node_id, str) else json.dumps(node_id, separators=(",", ":"))
            raise ValueError(f"invalid node_id: {label}")
        if node_id in nodes:
            raise ValueError(f"duplicate node_id: {node_id}")
        nodes[node_id] = f"{profile}/compute/nodes/{node_id}.tfstate"
    return {"shared": f"{profile}/compute/shared.tfstate", "nodes": nodes}


def expand(topology: list[dict]) -> list[dict]:
    if not topology:
        raise ValueError("topology must declare at least one role")
    roles = set()
    requests = []
    for declaration in topology:
        role = declaration.get("role")
        if role is not None and (
            not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", role)
        ):
            raise ValueError("invalid role")
        if role in roles:
            raise ValueError("duplicate role")
        roles.add(role)
        if role is None and len(topology) > 1:
            raise ValueError("a null role must be the only role")
        count = declaration.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError("count must be a positive integer")
        for index in range(count):
            node_id = f"{role}-{index}" if role is not None else str(index)
            if not _safe(node_id):
                raise ValueError(f"invalid node_id: {node_id}")
            requests.append({"node_id": node_id, "role": role, "index": index})
    return requests


def collect(requests: list[dict], results: list[dict], entry_node_id: str) -> dict:
    if not requests:
        raise ValueError("no nodes requested")
    expected = {}
    for request in requests:
        node_id = request["node_id"]
        if node_id in expected:
            raise ValueError(f"duplicate requested node: {node_id}")
        expected[node_id] = request
    if entry_node_id not in expected:
        raise ValueError(f"unknown entry node: {entry_node_id}")
    received = {}
    for result in results:
        node_id = result.get("node_id")
        if node_id not in expected:
            raise ValueError(f"undeclared node: {'null' if node_id is None else node_id}")
        if node_id in received:
            raise ValueError(f"duplicate node: {node_id}")
        received[node_id] = result
    nodes = []
    provider = None
    for node_id, request in expected.items():
        if node_id not in received:
            raise ValueError(f"missing node: {node_id}")
        result = received[node_id]
        required = ["provider", "name", "ip", "user", "sudoer"]
        if request.get("private") is True:
            required.append("vpc_ip")
        for field in required:
            if not isinstance(result.get(field), str) or not result[field].strip():
                raise ValueError(f"incomplete node {node_id}: {field}")
        provider = provider or result["provider"]
        if result["provider"] != (request.get("provider") or provider) or result["provider"] != provider:
            raise ValueError(f"provider mismatch: {node_id}")
        nodes.append({**result, "role": request.get("role"), "index": request.get("index")})
    return {"provider": provider, "entry_node_id": entry_node_id, "nodes": nodes}


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
