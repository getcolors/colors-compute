"""Private conditional S3/R2 journal transport. No retries or provider dispatch."""
import json
import os
from pathlib import Path
import re
import tempfile

from .backend import _run
from .contract import _missing, _safe
from .coordination import _document, _nonblank, _shape
from .rendering import backend_plan

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


def _json(value):
    return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))


def _contains_secret(value, credentials):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    return any(secret in encoded or json.dumps(secret, ensure_ascii=False)[1:-1] in encoded
               for secret in credentials)


def _write(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(content)


def _settings(opts):
    if not _safe(opts.get('profile')):
        raise ValueError('invalid profile')
    key = opts['profile'] + '/compute/coordination.json'
    config = backend_plan(opts, key)['config']['terraform']['backend']['s3']
    return config


def _identity(opts, settings):
    backend = {'kind': opts['provider-backend'], 'bucket': settings['bucket'], 'region': settings['region']}
    if backend['kind'] == 'r2':
        backend['endpoint'] = settings['endpoints']['s3']
    return {'profile': opts['profile'], 'provider': opts.get('provider-compute'), 'backend': backend}


def _service_error(stderr, operation):
    if not isinstance(stderr, str):
        return None
    match = re.match(r'\s*(?:aws: \[ERROR\]: )?An error occurred \(([^()\s]+)\) when calling the ' + re.escape(operation) + r' operation(?: \(reached max retries: [0-9]+\))?:', stderr)
    return match[1] if match else None


def _etag(output):
    metadata = _json(output)
    if not isinstance(metadata, dict) or not _nonblank(metadata.get('ETag')):
        raise ValueError('invalid metadata')
    return metadata['ETag']


def _environment(opts, source, path):
    is_r2 = opts['provider-backend'] == 'r2'
    child = {key: value for key, value in source.items()
             if not key.startswith('COLORS_PAR_')
             and (not is_r2 or not key.startswith('AWS_') or key == 'AWS_CA_BUNDLE')}
    if is_r2:
        credentials = [source.get('COLORS_PAR_R2_ACCESS_KEY_ID'), source.get('COLORS_PAR_R2_SECRET_ACCESS_KEY')]
        if any(not isinstance(value, str) or _missing(value) or '\n' in value or '\r' in value for value in credentials):
            raise ValueError('invalid credentials')
        credentials_file, config_file = path / 'credentials', path / 'config'
        _write(credentials_file, ('[default]\naws_access_key_id = ' + credentials[0] +
               '\naws_secret_access_key = ' + credentials[1] + '\n').encode())
        _write(config_file, b'')
        child.update(AWS_SHARED_CREDENTIALS_FILE=str(credentials_file), AWS_CONFIG_FILE=str(config_file),
                     AWS_REQUEST_CHECKSUM_CALCULATION='when_required', AWS_RESPONSE_CHECKSUM_VALIDATION='when_required')
    child.update(AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off', AWS_MAX_ATTEMPTS='1')
    return child


async def _session(opts, operation, intent, environment, runner):
    try:
        settings = _settings(opts)
        payload = None
        if operation == 'PutObject':
            if not _shape(intent, ('condition', 'document')) or not _document(intent['document']):
                return {'status': 'error'}
            if intent['document']['identity'] != _identity(opts, settings):
                return {'status': 'error'}
            condition = intent['condition']
            if not ((_shape(condition, ('if_none_match',)) and condition['if_none_match'] == '*') or
                    (_shape(condition, ('if_match',)) and _nonblank(condition['if_match']))):
                return {'status': 'error'}
            payload = json.dumps(intent['document'], ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(payload) > MAX_DOCUMENT_BYTES:
                return {'status': 'error'}
        source = dict(os.environ if environment is None else environment)
        credentials = [source.get(name) for name in ('COLORS_PAR_R2_ACCESS_KEY_ID', 'COLORS_PAR_R2_SECRET_ACCESS_KEY')] if opts['provider-backend'] == 'r2' else []
        if any(not isinstance(value, str) or _missing(value) or '\n' in value or '\r' in value for value in credentials):
            return {'status': 'error'}
        if payload is not None and _contains_secret(intent, credentials):
            return {'status': 'error'}
        with tempfile.TemporaryDirectory(prefix='colors-compute-journal-') as directory:
            path = Path(directory)
            os.chmod(path, 0o700)
            child = _environment(opts, source, path)
            body = path / 'document.json'
            _write(body, b'' if payload is None else payload)
            command = ['aws', 's3api', 'get-object' if operation == 'GetObject' else 'put-object',
                       '--bucket', settings['bucket'], '--key', settings['key']]
            if operation == 'GetObject':
                command.append(str(body))
            else:
                command.extend(['--body', str(body), '--content-type', 'application/json'])
                if 'if_match' in intent['condition']:
                    command.extend(['--if-match', intent['condition']['if_match']])
                else:
                    command.extend(['--if-none-match', '*'])
            command.extend(['--region', settings['region'], '--output', 'json', '--no-cli-pager'])
            if opts['provider-backend'] == 'r2':
                command.extend(['--endpoint-url', settings['endpoints']['s3']])
            if _contains_secret(command, credentials):
                return {'status': 'error'}
            result = await (runner or _run)(command, directory, child, 120000)
            if result.exit != 0:
                code = _service_error(result.err, operation)
                if operation == 'GetObject' and code == 'NoSuchKey':
                    return {'status': 'absent'}
                if operation == 'PutObject' and code in ('PreconditionFailed', 'ConditionalRequestConflict'):
                    return {'status': 'conflict'}
                return {'status': 'error'}
            etag = _etag(result.out)
            if _contains_secret(etag, credentials):
                return {'status': 'error'}
            if operation == 'PutObject':
                return {'status': 'written', 'etag': etag}
            if body.stat().st_size > MAX_DOCUMENT_BYTES:
                return {'status': 'error'}
            document = _json(body.read_bytes().decode('utf-8'))
            if not isinstance(document, dict) or _contains_secret(document, credentials):
                return {'status': 'error'}
            return {'status': 'present', 'etag': etag, 'document': document}
    except Exception:
        return {'status': 'error'}


async def journal_get(opts, environment=None, runner=None):
    """Read an untrusted journal; only exact GetObject NoSuchKey proves absence."""
    return await _session(opts, 'GetObject', None, environment, runner)


async def journal_put(opts, intent, environment=None, runner=None):
    """Attempt one conditional write; error may mean ambiguous completion."""
    return await _session(opts, 'PutObject', intent, environment, runner)
