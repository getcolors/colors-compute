"""Private subprocess execution and strict OpenTofu state decoding."""
import asyncio
import json
import os
import signal
from pathlib import Path
from typing import NamedTuple


class ProcessResult(NamedTuple):
    exit: int
    out: str
    err: str = ""
    command_reason: str | None = None


async def _run(command, cwd, environment, timeout_ms):
    program = str(command[0])
    candidates = ([Path(cwd) / program] if "/" in program else
                  [Path(cwd) / part / program for part in environment["PATH"].split(os.pathsep)]
                  if "PATH" in environment else [])
    if not Path(cwd).is_dir():
        return ProcessResult(-1, "", "", "process_start_failed")
    resolved = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    if resolved is None:
        reason = "process_start_failed" if any(path.exists() for path in candidates) else "executable_not_found"
        return ProcessResult(-1, "", "", reason)
    try:
        process = await asyncio.create_subprocess_exec(
            str(resolved.absolute()), *command[1:], cwd=cwd, env=environment,
            start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, umask=0o077,
        )
    except OSError:
        return ProcessResult(-1, "", "", "process_start_failed")
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout_ms / 1000)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        if isinstance(error, TimeoutError):
            return ProcessResult(-1, "", "", "timeout")
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


def _outputs(output):
    _params(output)
    result = {}
    for name, entry in json.loads(output)['outputs'].items():
        if not isinstance(entry, dict) or 'value' not in entry or entry.get('sensitive', False) is not False:
            raise ValueError('invalid outputs')
        result[name] = entry['value']
    return result
