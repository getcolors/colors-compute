"""Deterministic template and backend plans without credential access."""
from ._copy import deepcopy
from importlib.resources import files
import json
import re

from .contract import _missing, registry


def provider_plan(provider: str, stage: str, inputs: dict) -> dict:
    templates = json.loads(files("colors_compute").joinpath("templates.json").read_text())
    if not isinstance(provider, str) or provider not in templates:
        raise ValueError("compute provider templates unavailable: " + (provider if isinstance(provider, str) else json.dumps(provider, separators=(",", ":"))))
    if not isinstance(stage, str) or stage not in templates[provider]:
        raise ValueError("unsupported compute stage: " + (stage if isinstance(stage, str) else json.dumps(stage, separators=(",", ":"))))
    return render_template(templates[provider][stage], inputs)


def render_template(value, inputs: dict):
    if isinstance(value, str):
        token = re.fullmatch(r"\{\{([a-z_]+)\}\}", value)
        if token:
            if token[1] not in inputs:
                raise ValueError(f"missing template input: {token[1]}")
            return deepcopy(inputs[token[1]])
        if "{{" in value or "}}" in value:
            raise ValueError("template placeholders must occupy the entire string")
        return value
    if isinstance(value, list):
        return [render_template(item, inputs) for item in value]
    if isinstance(value, dict):
        return {key: render_template(item, inputs) for key, item in value.items()}
    return value


def backend_plan(opts: dict, state_key: str) -> dict:
    backend = opts.get("provider-backend")
    backends = registry()["backend"]
    if not isinstance(backend, str) or backend not in backends:
        raise ValueError(":provider-backend must be one of r2, s3")
    for key in sorted(backends[backend]["required"]):
        if _missing(opts.get(key)):
            raise ValueError(f":{key} is required")
    if not isinstance(state_key, str) or not state_key or any(
        not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", part)
        for part in state_key.split("/")
    ):
        raise ValueError("invalid state key")
    config = {"bucket": opts[f"{backend}-bucket"], "key": state_key,
              "region": opts["s3-region"] if backend == "s3" else "auto", "use_lockfile": True}
    if backend == "r2":
        config.update({"endpoints": {"s3": opts["r2-endpoint"]}, "use_path_style": False,
                       "skip_credentials_validation": True, "skip_metadata_api_check": True,
                       "skip_region_validation": True, "skip_requesting_account_id": True,
                       "skip_s3_checksum": True})
    bindings = {"COLORS_PAR_" + key.upper().replace("-", "_"): option
                for key, option in backends[backend].get("backend-config", {}).items()}
    return {"config": {"terraform": {"backend": {"s3": config}}},
            "credential_bindings": bindings, "environment": {}}
