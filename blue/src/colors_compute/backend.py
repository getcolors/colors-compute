"""Private, read-only OpenTofu backend sessions. Never infer state absence."""
import asyncio
import json
import os
import signal
from pathlib import Path
import tempfile
from typing import NamedTuple

from .contract import _missing
from .rendering import backend_plan


class ProcessResult(NamedTuple):
    exit: int
    out: str
    err: str = ""


async def _run(command, cwd, environment, timeout_ms):
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, env=environment, start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout_ms / 1000)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        raise
    return ProcessResult(process.returncode, out.decode("utf-8"), err.decode("utf-8", errors="replace"))


def _write_private(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream)


def _params(output):
    def reject_constant(_):
        raise ValueError("invalid state")

    state = json.loads(output, parse_constant=reject_constant)
    if (not isinstance(state, dict)
            or type(state.get("version")) not in (int, float) or state["version"] != 4
            or type(state.get("serial")) not in (int, float)
            or not 0 <= state["serial"] <= 9007199254740991
            or state["serial"] != int(state["serial"])
            or not isinstance(state.get("lineage"), str) or not state["lineage"].strip()
            or not isinstance(state.get("outputs"), dict)
            or not isinstance(state.get("resources"), list)):
        raise ValueError("invalid state")
    if "params" not in state["outputs"]:
        return {}
    params = state["outputs"]["params"]
    if not isinstance(params, dict) or not isinstance(params.get("value"), dict):
        raise ValueError("invalid params")
    return params["value"]


async def read_state(opts, state_key, environment=None, runner=None):
    """Return present params or a generic error; callers must not log params.

    Runner receives (argv, cwd, exact_environment, timeout_ms). No mutations are
    available. A failed read cannot authorize creation or deletion.
    """
    try:
        source = dict(os.environ if environment is None else environment)
        plan = backend_plan(opts, state_key)
        credentials = {}
        for variable, setting in plan["credential_bindings"].items():
            value = source.get(variable)
            if not isinstance(value, str) or _missing(value):
                return {"status": "error"}
            credentials[setting] = value
        child_env = {key: value for key, value in source.items()
                     if not key.startswith(("TF_", "TOFU_", "COLORS_PAR_"))}
        if opts.get("provider-backend") == "r2":
            child_env.pop("AWS_PROFILE", None)
            child_env.pop("AWS_DEFAULT_PROFILE", None)
        execute = runner or _run
        with tempfile.TemporaryDirectory(prefix="colors-compute-") as directory:
            os.chmod(directory, 0o700)
            path = Path(directory)
            _write_private(path / "backend.tf.json", plan["config"])
            credential_file = path / "credentials.tfbackend.json"
            _write_private(credential_file, credentials)
            child_env.update(TF_IN_AUTOMATION="1", TF_INPUT="0", TF_WORKSPACE="default",
                             TF_DATA_DIR=str(path / ".terraform"))
            init = await execute(["tofu", "init", "-input=false", "-no-color", "-reconfigure",
                                  f"-backend-config={credential_file}"], directory, child_env, 120000)
            if init.exit != 0:
                return {"status": "error"}
            pull = await execute(["tofu", "state", "pull"], directory, child_env, 120000)
            if pull.exit != 0:
                return {"status": "error"}
            params = _params(pull.out)
            encoded = json.dumps(params, ensure_ascii=False)
            if any(secret in encoded or json.dumps(secret, ensure_ascii=False)[1:-1] in encoded
                   for secret in credentials.values()):
                return {"status": "error"}
            return {"status": "present", "params": params}
    except Exception:
        # Diagnostics can contain backend secrets and raw state. Never forward.
        return {"status": "error"}
