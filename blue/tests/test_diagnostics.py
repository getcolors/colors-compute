import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from colors_compute.backend import ProcessResult
from colors_compute.diagnostics import sanitize_stderr, credential_values
from colors_compute.node import compute_node, node_plan
from test_node import inputs, LocalRunner


def local_inputs(tmp_path):
    opts, request = inputs(tmp_path)
    opts['provider-backend'] = 'local'
    return opts, request


@pytest.mark.asyncio
async def test_asdf_init_failure_exposes_safe_actionable_diagnostics(tmp_path):
    opts, request = local_inputs(tmp_path)
    shims = tmp_path / 'shims'
    shims.mkdir()
    executable = shims / 'tofu'
    executable.write_text('#!/bin/sh\n')
    executable.chmod(0o700)
    stderr = 'No version is set for command tofu\nConsider adding one of the following versions in your config file at /workspace/.tool-versions\nopentofu 1.11.2\n'
    async def runner(args, cwd, env, timeout):
        assert args[:2] == ['tofu', 'init']
        return ProcessResult(126, 'stdout must never be returned', stderr)
    result = await compute_node(opts, request, environment={'PATH': str(shims), 'COLORS_PAR_DO_TOKEN': 'known-token'}, dependencies={'runner': runner})
    assert result == {'status': 'error', 'error': {
        'code': 'command_failed', 'stage': 'init', 'message': 'Required command failed.',
        'infrastructure_changes': 'none', 'command': ['tofu', 'init'],
        'executable': str(executable), 'exit_code': 126, 'stderr': stderr}}
    assert not (Path(node_plan(opts, request)['directory']) / 'credentials.tfbackend.json').exists()


@pytest.mark.asyncio
async def test_apply_failure_reports_possible_changes_and_redacts(tmp_path):
    opts, request = local_inputs(tmp_path)
    class ApplyFailure(LocalRunner):
        async def __call__(self, args, cwd, env, timeout):
            if args[:2] == ['tofu', 'apply']:
                return ProcessResult(1, 'private stdout', '\x1b[31mprovider rejected known-token\x1b[0m\npassword=unknown-value\n')
            return await super().__call__(args, cwd, env, timeout)
    result = await compute_node(opts, request, environment={'PATH': '/nonexistent', 'COLORS_PAR_DO_TOKEN': 'known-token'}, dependencies={'runner': ApplyFailure(opts, request)})
    assert result == {'status': 'error', 'error': {
        'code': 'command_failed', 'stage': 'apply', 'message': 'Required command failed.',
        'infrastructure_changes': 'possible', 'command': ['tofu', 'apply'], 'exit_code': 1,
        'stderr': 'provider rejected [REDACTED]\npassword=[REDACTED]\n'}}


@pytest.mark.asyncio
async def test_missing_credentials_and_malformed_state_are_distinct(tmp_path):
    opts, request = local_inputs(tmp_path)
    result = await compute_node(opts, request, environment={})
    assert result == {'status': 'error', 'error': {
        'code': 'missing_credentials', 'stage': 'credentials', 'message': 'Required credentials are not set.',
        'infrastructure_changes': 'none', 'credential': 'COLORS_PAR_DO_TOKEN'}}
    root = Path(node_plan(opts, request)['directory'])
    (root / request['state_filename']).write_text('{malformed contains secret material')
    result = await compute_node(opts, request, environment={'COLORS_PAR_DO_TOKEN': 'known-token'}, dependencies={'runner': LocalRunner(opts, request)})
    assert result == {'status': 'error', 'error': {
        'code': 'state_unreadable', 'stage': 'state', 'message': 'Compute state could not be read.',
        'infrastructure_changes': 'none'}}


@pytest.mark.asyncio
async def test_invalid_operation_diagnostics_do_not_touch_workdir(tmp_path):
    opts, request = local_inputs(tmp_path)
    result = await compute_node(opts, request, 'bad-operation', environment={})
    assert result == {'status': 'error', 'error': {
        'code': 'invalid_request', 'stage': 'validate', 'message': 'Invalid compute request.',
        'infrastructure_changes': 'none'}}
    assert not (tmp_path / opts['profile']).exists()


def test_stderr_redacts_pem_before_known_tokens_encoded_secrets_and_dumps():
    pem = '-----BEGIN OPENSSH PRIVATE KEY-----\nunknown-body-do-not-leak\n-----END OPENSSH PRIVATE KEY-----'
    assert sanitize_stderr(pem, {'OPENSSH'}) == '[private material suppressed]'
    secret = 'credential/value+"'
    variants = [secret, quote(secret, safe=''), base64.b64encode(secret.encode()).decode(), json.dumps(secret)[1:-1]]
    assert sanitize_stderr('\n'.join(variants), {secret}) == '\n'.join(['[REDACTED]'] * 4)
    assert sanitize_stderr('{"resources":[{"private_key":"opaque"}]}', set()) == '[structured output suppressed]'
    assert sanitize_stderr('Authorization: Bearer unknown-token\n', set()) == 'Authorization: [REDACTED]\n'
    assert len(sanitize_stderr('x' * 5000, set())) == 2000
    assert '\x1b' not in sanitize_stderr('\x1b[31merror\x1b[0m\x00', set())


@pytest.mark.asyncio
@pytest.mark.parametrize('at', ['init', 'apply'])
async def test_cancellation_propagates_and_retains_templates(tmp_path, at):
    opts, request = local_inputs(tmp_path)
    class Cancelled(LocalRunner):
        async def __call__(self, args, cwd, env, timeout):
            if args[:2] == ['tofu', at]:
                raise asyncio.CancelledError()
            return await super().__call__(args, cwd, env, timeout)
    with pytest.raises(asyncio.CancelledError):
        await compute_node(opts, request, environment={'COLORS_PAR_DO_TOKEN': 'known-token'}, dependencies={'runner': Cancelled(opts, request)})
    root = Path(node_plan(opts, request)['directory'])
    assert (root / 'compute.tf.json').exists()
    assert not (root / 'credentials.tfbackend.json').exists()


def test_clojure_maps_and_api_keys_are_suppressed():
    assert sanitize_stderr('error: {:password "unknown"}', set()) == '[structured output suppressed]'
    assert sanitize_stderr('{:resources [{:private_key "opaque"}]}', set()) == '[structured output suppressed]'
    secrets = credential_values({'API_KEY': 'opaque-api-value'}, {'provider-api-key': 'second-api-value'})
    assert secrets == {'opaque-api-value', 'second-api-value'}
    assert sanitize_stderr('provider says opaque-api-value', secrets) == 'provider says [REDACTED]'
    assert sanitize_stderr('api_key=unknown-api-value', set()) == 'api_key=[REDACTED]'
