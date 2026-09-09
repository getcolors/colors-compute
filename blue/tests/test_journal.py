import asyncio
from copy import deepcopy
import json
from pathlib import Path
import stat

import pytest

from colors_compute.backend import ProcessResult
from colors_compute.coordination import coordination
from colors_compute.journal import journal_get, journal_put, MAX_DOCUMENT_BYTES

OPTS = {'profile': 'demo', 'provider-compute': 'vultr', 'provider-backend': 'r2',
        'r2-bucket': 'states', 'r2-endpoint': 'https://example.invalid'}
ENV = {'PATH': '/bin', 'HOME': '/home/fixture', 'AWS_CA_BUNDLE': '/fixture/ca.pem',
       'AWS_ACCESS_KEY_ID': 'ambient', 'AWS_SECRET_ACCESS_KEY': 'ambient-secret',
       'AWS_SESSION_TOKEN': 'ambient-token', 'AWS_PROFILE': 'profile', 'AWS_ENDPOINT_URL': 'https://wrong.invalid',
       'COLORS_PAR_R2_ACCESS_KEY_ID': 'fixture-access', 'COLORS_PAR_R2_SECRET_ACCESS_KEY': 'fixture-secret'}
IDENTITY = {'profile': 'demo', 'provider': 'vultr', 'backend': {'kind': 'r2', 'bucket': 'states', 'region': 'auto', 'endpoint': 'https://example.invalid'}}
INTENT = coordination({'status': 'absent'}, IDENTITY, {'type': 'acquire', 'run_id': 'run-1', 'write_id': 'write-1', 'target_etag': None})


def body_path(command):
    return Path(command[command.index('--body') + 1] if '--body' in command else command[7])


@pytest.mark.asyncio
async def test_private_r2_get_and_no_parent_environment_mutation():
    directories = []
    original = deepcopy(ENV)
    async def runner(command, cwd, child, timeout):
        directories.append(Path(cwd))
        assert stat.S_IMODE(Path(cwd).stat().st_mode) == 0o700
        for path in Path(cwd).iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert timeout == 120000
        assert child['AWS_CA_BUNDLE'] == ENV['AWS_CA_BUNDLE']
        assert not any(key in child for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_ENDPOINT_URL'))
        assert not any(key.startswith('COLORS_PAR_') for key in child)
        assert child['AWS_MAX_ATTEMPTS'] == '1'
        assert child['AWS_REQUEST_CHECKSUM_CALCULATION'] == 'when_required'
        assert child['AWS_RESPONSE_CHECKSUM_VALIDATION'] == 'when_required'
        assert Path(child['AWS_CONFIG_FILE']).read_text() == ''
        assert Path(child['AWS_SHARED_CREDENTIALS_FILE']).read_text() == '[default]\naws_access_key_id = fixture-access\naws_secret_access_key = fixture-secret\n'
        assert all(secret not in ' '.join(command) for secret in ('fixture-access', 'fixture-secret'))
        assert command[:7] == ['aws', 's3api', 'get-object', '--bucket', 'states', '--key', 'demo/compute/coordination.json']
        body_path(command).write_text(json.dumps(INTENT['document']))
        return ProcessResult(0, '{"ETag":"opaque"}')
    assert await journal_get(OPTS, ENV, runner) == {'status': 'present', 'etag': 'opaque', 'document': INTENT['document']}
    assert directories and not directories[0].exists()
    assert ENV == original


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', [{'if_none_match': '*'}, {'if_match': 'opaque-etag'}])
async def test_put_exact_condition_and_private_document(condition):
    intent = {**INTENT, 'condition': condition}
    async def runner(command, cwd, child, timeout):
        assert json.loads(body_path(command).read_text()) == INTENT['document']
        flag = '--if-match' if 'if_match' in condition else '--if-none-match'
        assert command[command.index(flag) + 1] == next(iter(condition.values()))
        return ProcessResult(0, '{"ETag":"new"}')
    assert await journal_put(OPTS, intent, ENV, runner) == {'status': 'written', 'etag': 'new'}


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,stderr,expected', [
    ('get', 'An error occurred (NoSuchKey) when calling the GetObject operation: missing', 'absent'),
    ('get', '\n An error occurred (NoSuchKey) when calling the GetObject operation: missing', 'absent'),
    ('get', 'An error occurred (NoSuchBucket) when calling the GetObject operation: missing', 'error'),
    ('get', 'An error occurred (AccessDenied) when calling the GetObject operation: NoSuchKey', 'error'),
    ('get', 'first line\nAn error occurred (NoSuchKey) when calling the GetObject operation: missing', 'error'),
    ('get', 'An error occurred (NoSuchKey) when calling the PutObject operation: missing', 'error'),
    ('get', '404 NoSuchKey', 'error'),
    ('get', '\naws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (reached max retries: 0): synthetic', 'absent'),
    ('put', '\naws: [ERROR]: An error occurred (PreconditionFailed) when calling the PutObject operation (reached max retries: 0): synthetic', 'conflict'),
    ('get', 'aws: [ERROR]: An error occurred (NoSuchKey) when calling the PutObject operation (reached max retries: 0): synthetic', 'error'),
    ('get', 'warning: aws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (reached max retries: 0): synthetic', 'error'),
    ('get', 'aws: [ERROR]: An error occurred (AccessDenied) when calling the GetObject operation (reached max retries: 0): NoSuchKey', 'error'),
    ('put', 'An error occurred (PreconditionFailed) when calling the PutObject operation: conflict', 'conflict'),
    ('put', 'An error occurred (ConditionalRequestConflict) when calling the PutObject operation: conflict', 'conflict'),
    ('put', 'An error occurred (AccessDenied) when calling the PutObject operation: PreconditionFailed', 'error'),
    ('put', 'An error occurred (PreconditionFailed) when calling the GetObject operation: conflict', 'error'),
])
async def test_exact_service_error_classification(operation, stderr, expected):
    paths = []
    async def runner(command, cwd, child, timeout):
        paths.append(Path(cwd))
        return ProcessResult(1, '', stderr)
    result = await (journal_get(OPTS, ENV, runner) if operation == 'get' else journal_put(OPTS, INTENT, ENV, runner))
    assert result == {'status': expected}
    assert not paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata,body', [('', '{}'), ('null', '{}'), ('{}', '{}'), ('{"ETag":" "}', '{}'),
    ('{"ETag":"ok"}', ''), ('{"ETag":"ok"}', '[]'), ('{"ETag":"ok"}', '{"n":NaN}'),
    ('{"ETag":"ok"}', '{"payload":"' + 'x' * MAX_DOCUMENT_BYTES + '"}')])
async def test_invalid_or_oversized_reads_fail_closed(metadata, body):
    async def runner(command, cwd, child, timeout):
        body_path(command).write_text(body)
        return ProcessResult(0, metadata)
    assert await journal_get(OPTS, ENV, runner) == {'status': 'error'}


@pytest.mark.asyncio
async def test_s3_ambient_chain_unchanged_and_no_r2_files():
    opts = {'profile': 'demo', 'provider-backend': 's3', 's3-bucket': 'states', 's3-region': 'eu-west-1'}
    async def runner(command, cwd, child, timeout):
        for key in ('AWS_PROFILE', 'AWS_SESSION_TOKEN', 'AWS_ACCESS_KEY_ID', 'AWS_ENDPOINT_URL'):
            assert child[key] == ENV[key]
        assert '--endpoint-url' not in command
        assert list(Path(cwd).iterdir()) == [body_path(command)]
        return ProcessResult(1, '', 'An error occurred (NoSuchKey) when calling the GetObject operation: missing')
    assert await journal_get(opts, ENV, runner) == {'status': 'absent'}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['', 'REPLACE_ME', 'value\nnew=value', 'value\rnew=value', None])
async def test_invalid_credentials_never_execute(bad):
    async def runner(*args):
        pytest.fail('unexpected execution')
    assert await journal_get(OPTS, {**ENV, 'COLORS_PAR_R2_SECRET_ACCESS_KEY': bad}, runner) == {'status': 'error'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['extra', 'condition', 'identity', 'node-secret'])
async def test_strict_put_intents_rejected_before_execute(mutation):
    intent = deepcopy(INTENT)
    if mutation == 'extra': intent['arbitrary'] = 'secret'
    if mutation == 'condition': intent['condition']['if_match'] = 'other'
    if mutation == 'identity': intent['document']['identity']['profile'] = 'other'
    if mutation == 'node-secret': intent['document']['lock']['password'] = 'secret'
    async def runner(*args):
        pytest.fail('unexpected execution')
    assert await journal_put(OPTS, intent, ENV, runner) == {'status': 'error'}


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_timeout_and_cancel_cleanup(cancel):
    paths = []
    async def runner(command, cwd, child, timeout):
        paths.append(Path(cwd))
        if cancel: raise asyncio.CancelledError()
        raise TimeoutError('private diagnostic')
    if cancel:
        with pytest.raises(asyncio.CancelledError): await journal_put(OPTS, INTENT, ENV, runner)
    else:
        assert await journal_put(OPTS, INTENT, ENV, runner) == {'status': 'error'}
    assert not paths[0].exists()

@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code,out,err', [
    (0, '', 'An error occurred (PreconditionFailed) when calling the PutObject operation: conflict'),
    (1, 'An error occurred (PreconditionFailed) when calling the PutObject operation: conflict', ''),
    (0, '{}', ''), (0, '{"ETag":false}', ''), (0, 'not JSON', ''),
])
async def test_put_ambiguous_results_never_claim_conflict_or_success(exit_code, out, err):
    calls = []
    async def runner(*args):
        calls.append(args)
        return ProcessResult(exit_code, out, err)
    assert await journal_put(OPTS, INTENT, ENV, runner) == {'status': 'error'}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_oversized_valid_document_rejected_before_execution():
    endpoint = 'https://' + 'a' * MAX_DOCUMENT_BYTES
    intent = deepcopy(INTENT)
    intent['document']['identity']['backend']['endpoint'] = endpoint
    async def runner(*args):
        pytest.fail('oversized write must not execute')
    assert await journal_put({**OPTS, 'r2-endpoint': endpoint}, intent, ENV, runner) == {'status': 'error'}

@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'\xff', '{}'.encode('utf-16'), b'\xef\xbb\xbf{}'])
async def test_body_requires_strict_utf8_without_bom(body):
    async def runner(command, cwd, child, timeout):
        body_path(command).write_bytes(body)
        return ProcessResult(0, '{"ETag":"opaque"}')
    assert await journal_get(OPTS, ENV, runner) == {'status': 'error'}


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['argument', 'document', 'etag', 'get-document'])
@pytest.mark.parametrize('secret', ['fixture-secret', 'fixture-quote"value'])
async def test_known_r2_credentials_never_enter_commands_or_results(target, secret):
    env = {**ENV, 'COLORS_PAR_R2_SECRET_ACCESS_KEY': secret}
    intent = deepcopy(INTENT)
    opts = deepcopy(OPTS)
    if target == 'argument':
        intent['condition'] = {'if_match': secret}
    elif target == 'document':
        # Safe identifiers cannot carry quote; a nested endpoint can carry only
        # the simple secret. Use write ID for the simple case and test escaped
        # secret through the ETag argument otherwise.
        if '"' in secret:
            intent['condition'] = {'if_match': secret}
        else:
            intent['document']['write_id'] = secret
    calls = []
    async def runner(command, cwd, child, timeout):
        calls.append(command)
        if target in ('argument', 'document'):
            pytest.fail('credential echo must be rejected before subprocess')
        if target == 'get-document':
            body_path(command).write_text(json.dumps({'echo': secret}))
        return ProcessResult(0, json.dumps({'ETag': secret if target == 'etag' else 'opaque'}))
    result = await (journal_get(opts, env, runner) if target == 'get-document' else journal_put(opts, intent, env, runner))
    assert result == {'status': 'error'}
    assert len(calls) == (0 if target in ('argument', 'document') else 1)
