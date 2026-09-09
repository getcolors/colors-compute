import asyncio
import json
from pathlib import Path
import stat

import pytest

from colors_compute.backend import ProcessResult, read_state

OPTS = {"provider-backend": "r2", "r2-bucket": "test", "r2-endpoint": "https://example.invalid"}
ENV = {"COLORS_PAR_R2_ACCESS_KEY_ID": "backend-access-example",
       "COLORS_PAR_R2_SECRET_ACCESS_KEY": "backend-secret-example",
       "AWS_ACCESS_KEY_ID": "ambient-aws", "AWS_SECRET_ACCESS_KEY": "ambient-secret",
       "AWS_PROFILE": "missing-profile", "AWS_DEFAULT_PROFILE": "missing-default",
       "HOME": "/example/home", "PATH": "/bin", "TF_LOG": "TRACE",
       "TF_CLI_ARGS_init": "-migrate-state", "TOFU_LOG": "TRACE",
       "COLORS_PAR_VULTR_API_KEY": "unused-compute-secret"}
STATE = {"version": 4, "serial": 0, "lineage": "lineage", "outputs": {
    "params": {"value": {"provider": "vultr", "ip": "192.0.2.1"}}}, "resources": []}


@pytest.mark.asyncio
async def test_private_r2_session_preserves_ambient_aws_and_cleans_cache():
    calls = []

    async def run(command, cwd, env, timeout):
        path = Path(cwd)
        calls.append((command, path))
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for filename in ("backend.tf.json", "credentials.tfbackend.json"):
            assert stat.S_IMODE((path / filename).stat().st_mode) == 0o600
        config = (path / "backend.tf.json").read_text()
        creds = json.loads((path / "credentials.tfbackend.json").read_text())
        assert creds == {"access_key": ENV["COLORS_PAR_R2_ACCESS_KEY_ID"],
                         "secret_key": ENV["COLORS_PAR_R2_SECRET_ACCESS_KEY"]}
        assert creds["secret_key"] not in config + " ".join(command)
        assert env["AWS_ACCESS_KEY_ID"] == "ambient-aws"
        assert env["AWS_SECRET_ACCESS_KEY"] == "ambient-secret"
        assert env["HOME"] == ENV["HOME"]
        assert "AWS_PROFILE" not in env and "AWS_DEFAULT_PROFILE" not in env
        assert "TF_LOG" not in env and "TF_CLI_ARGS_init" not in env and "TOFU_LOG" not in env
        assert not any(k.startswith("COLORS_PAR_") for k in env)
        assert env["TF_WORKSPACE"] == "default"
        assert env["TF_DATA_DIR"] == str(path / ".terraform")
        assert timeout == 120000
        if command[1] == "init":
            cache = path / ".terraform"
            cache.mkdir()
            (cache / "terraform.tfstate").write_text("cached secret")
        return ProcessResult(0, json.dumps(STATE))

    assert await read_state(OPTS, "test/compute/shared.tfstate", ENV, run) == {
        "status": "present", "params": STATE["outputs"]["params"]["value"]}
    assert len(calls) == 2
    assert calls[0][0][:5] == ["tofu", "init", "-input=false", "-no-color", "-reconfigure"]
    assert calls[1][0] == ["tofu", "state", "pull"]
    assert not calls[0][1].exists()
    assert ENV["TF_LOG"] == "TRACE"


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["", "not JSON", "null", "[]", "{}",
    json.dumps({**STATE, "version": True}), json.dumps({**STATE, "serial": -1}),
    json.dumps({**STATE, "serial": 9007199254740992}), json.dumps({**STATE, "lineage": " "}),
    json.dumps({**STATE, "resources": {}}), json.dumps({**STATE, "outputs": []}),
    json.dumps({**STATE, "outputs": {"params": {"value": None}}}),
    json.dumps({**STATE, "outputs": {"params": {"value": {"secret": ENV["COLORS_PAR_R2_SECRET_ACCESS_KEY"]}}}})])
async def test_failed_state_never_becomes_absent(output):
    paths = []
    async def run(command, cwd, env, timeout):
        paths.append(Path(cwd))
        return ProcessResult(0, output)
    assert await read_state(OPTS, "test/state", ENV, run) == {"status": "error"}
    assert paths and not paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [-1, 1, 127])
@pytest.mark.parametrize("stage", ["init", "state"])
async def test_nonzero_and_timeout_exit_codes_fail_closed(exit_code, stage):
    paths = []
    async def run(command, cwd, env, timeout):
        paths.append(Path(cwd))
        return ProcessResult(exit_code if command[1] == stage else 0, json.dumps(STATE), "secret diagnostics")
    assert await read_state(OPTS, "test/state", ENV, run) == {"status": "error"}
    assert len(paths) == (1 if stage == "init" else 2)
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_s3_legacy_state_without_params_is_present():
    async def run(command, cwd, env, timeout):
        assert json.loads((Path(cwd) / "credentials.tfbackend.json").read_text()) == {}
        return ProcessResult(0, json.dumps({**STATE, "outputs": {}}))
    opts = {"provider-backend": "s3", "s3-bucket": "test", "s3-region": "us-east-1"}
    assert await read_state(opts, "test/state", {}, run) == {"status": "present", "params": {}}


@pytest.mark.asyncio
async def test_missing_credentials_does_not_start_process():
    async def run(*args):
        pytest.fail("must reject before execution")
    assert await read_state(OPTS, "test/state", {}, run) == {"status": "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_exception_cleanup_and_cancellation(cancel):
    paths = []
    async def run(command, cwd, env, timeout):
        paths.append(Path(cwd))
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("backend-secret-example")
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await read_state(OPTS, "test/state", ENV, run)
    else:
        assert await read_state(OPTS, "test/state", ENV, run) == {"status": "error"}
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_default_runner_has_exact_environment(tmp_path, monkeypatch):
    import os
    import sys
    from colors_compute.backend import _run
    monkeypatch.setenv("TF_LOG", "secret inherited setting")
    result = await _run([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
                        str(tmp_path), {"AWS_ACCESS_KEY_ID": "ambient-key"}, 2000)
    assert result.exit == 0
    env = json.loads(result.out)
    assert "TF_LOG" not in env
    assert env["AWS_ACCESS_KEY_ID"] == "ambient-key"


@pytest.mark.asyncio
async def test_timeout_kills_child_processes_holding_output_pipes(tmp_path):
    import os
    import sys
    from colors_compute.backend import _run
    command = [sys.executable, "-c",
               "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"]
    import time
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_run(command, str(tmp_path), dict(os.environ), 100), 3000 / 1000)
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_escaped_backend_credential_cannot_be_returned():
    secret = 'quote"slash\\newline\nsecret'
    env = {**ENV, "COLORS_PAR_R2_SECRET_ACCESS_KEY": secret}
    state = {**STATE, "outputs": {"params": {"value": {"leak": secret}}}}
    async def run(*args):
        return ProcessResult(0, json.dumps(state))
    assert await read_state(OPTS, "test/state", env, run) == {"status": "error"}
