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
    assert sanitize_stderr('error: {:password "unknown"}', set()) == 'error: [structured output suppressed]'
    assert sanitize_stderr('{:resources [{:private_key "opaque"}]}', set()) == '[structured output suppressed]'
    secrets = credential_values({'API_KEY': 'opaque-api-value'}, {'provider-api-key': 'second-api-value'})
    assert secrets == {'opaque-api-value', 'second-api-value'}
    assert sanitize_stderr('provider says opaque-api-value', secrets) == 'provider says [REDACTED]'
    assert sanitize_stderr('api_key=unknown-api-value', set()) == 'api_key=[REDACTED]'

@pytest.mark.parametrize('case', json.loads((Path(__file__).parents[2] / 'test/fixtures/reauth.json').read_text()), ids=lambda case: case['name'])
def test_google_reauth_metadata(case):
    from colors_compute.diagnostics import command_metadata, NodeError, failure
    result = ProcessResult(case['result']['exit'], case['result'].get('out', ''), case['result']['err'])
    details = command_metadata(case['argv'], {}, '/tmp', result, case['provider'])
    error = failure(NodeError('command_failed', **details), 'plan', 'none', set())
    assert error['error'].get('auth_reason') == case['expected']
    assert 'PRIVATE-CANARY' not in json.dumps(error)
    assert 'PRIVATE-STDOUT' not in json.dumps(error)

@pytest.mark.asyncio
async def test_connection_resolver_returns_google_reauth_reason(tmp_path):
    from colors_compute.node import resolve_connection
    fixtures = json.loads((Path(__file__).parents[2] / 'test/fixtures/provider-requests.json').read_text())
    opts, _, source, *_ = next(case['args'] for case in fixtures if case['args'][0]['provider-compute'] == 'google' and case['args'][1] == 'shared')
    opts = {**opts, 'provider-backend': 'local'}
    _, request = inputs(tmp_path)
    request = {**request, 'security': source['security'], 'network': source['network']}
    request.pop('ssh_registration', None)
    plan = node_plan(opts, request)
    state = {'version': 4, 'serial': 1, 'lineage': 'reauth-fixture', 'resources': [{'type': 'google_compute_instance', 'mode': 'managed', 'name': 'node', 'instances': [{'attributes': {'instance_id': '123'}}]}],
             'outputs': {'compute_identity': plan['documents']['compute.tf.json']['output']['compute_identity'], 'params': {'value': {'provider': 'google', 'node_id': request['node_id'], 'name': 'node', 'ip': '192.0.2.1', 'user': 'ubuntu', 'sudoer': 'ubuntu', 'provider_id': '123'}}}}
    Path(plan['directory']).mkdir(parents=True, exist_ok=True)
    (Path(plan['directory']) / request['state_filename']).write_text(json.dumps(state))
    calls = []
    async def runner(args, cwd, env, timeout):
        calls.append(args[1])
        if args[1] == 'plan':
            return ProcessResult(1, 'PRIVATE-STDOUT', 'oauth2: invalid_grant invalid_rapt\n{"private_key":"PRIVATE-CANARY"}')
        return ProcessResult(0, json.dumps(state) if args[1] == 'state' else '', '')
    result = await resolve_connection(opts, request, {}, {'runner': runner})
    assert result['error'].get('auth_reason') == 'google_reauth_required', (result, calls)
    assert result['error']['infrastructure_changes'] == 'none'
    assert calls == ['init', 'plan']  # Local backend reads its state file directly.
    assert 'PRIVATE-CANARY' not in json.dumps(result)


@pytest.mark.parametrize('case', [case for case in json.loads((Path(__file__).parents[2] / 'test/fixtures/errors.json').read_text()) if case['op'] == 'sanitize_error'], ids=lambda case: case['name'])
def test_shared_sanitizer_fixtures(case):
    value, opts, environment = case['args']
    assert (sanitize_stderr(value, credential_values(opts, environment)) or None) == case['expected']
