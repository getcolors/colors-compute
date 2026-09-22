"""Named encrypted SSH resources and caller-scoped agent sessions."""
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse
from .contract import _safe, _local_path


def ssh_plan(opts, request):
    if not isinstance(request, dict) or set(request) - {'name', 'workdir', 'passphrase_env', 'backend', 'expected', 'new_passphrase_env', 'allow_delete', 'consumers_destroyed', 'lock_token'}:
        raise ValueError('invalid SSH request')
    if not _safe(opts.get('profile')) or not _safe(request.get('name')) or not _local_path(request.get('workdir')):
        raise ValueError('invalid SSH resource identity')
    if not isinstance(request.get('passphrase_env'), str) or not re.fullmatch(r'COLORS_PAR_[A-Z][A-Z0-9_]*', request['passphrase_env']):
        raise ValueError('invalid passphrase binding')
    override = request.get('backend', {})
    allowed = {'provider-backend', 's3-prefix', 's3-bucket', 's3-region', 'r2-bucket', 'r2-endpoint', 'oci-bucket', 'oci-region', 'oci-namespace', 'gcs-bucket'}
    if not isinstance(override, dict) or set(override) - allowed:
        raise ValueError('invalid SSH backend override')
    settings = {**opts, **override}
    kind = settings.get('provider-backend')
    if kind not in ('local', 's3', 'r2', 'oci', 'gcs'):
        raise ValueError('invalid SSH backend')
    prefix = settings.get('s3-prefix', '')
    if not isinstance(prefix, str) or prefix and any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', p) for p in prefix.split('/')):
        raise ValueError('invalid SSH prefix')
    directory = str(Path(request['workdir']) / opts['profile'] / 'ssh' / request['name'])
    key = '/'.join(p for p in (prefix, opts['profile'], 'ssh', request['name'], 'resource.json') if p)
    storage = {'kind': kind}
    if kind == 'local':
        storage['path'] = directory + '/resource.json'
    else:
        bucket = settings.get(kind + '-bucket')
        if not isinstance(bucket, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{1,221}', bucket):
            raise ValueError('invalid SSH bucket')
        storage['bucket'] = bucket
        if kind != 'gcs':
            region = 'auto' if kind == 'r2' else settings.get(kind + '-region')
            if not isinstance(region, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', region):
                raise ValueError('invalid SSH region')
            storage['region'] = region
        if kind in ('r2', 'oci'):
            if kind == 'oci' and (not isinstance(settings.get('oci-namespace'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', settings['oci-namespace'])):
                raise ValueError('invalid OCI namespace')
            endpoint = settings.get('r2-endpoint') if kind == 'r2' else 'https://' + str(settings.get('oci-namespace', '')) + '.compat.objectstorage.' + storage['region'] + '.oraclecloud.com'
            if not isinstance(endpoint, str) or not re.fullmatch(r'https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?/?', endpoint):
                raise ValueError('invalid SSH endpoint')
            parsed = urlparse(endpoint)
            if parsed.scheme != 'https' or not parsed.hostname or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label) for label in parsed.hostname.split('.')) or parsed.port is not None and not 1 <= parsed.port <= 65535 or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or any(c.isspace() for c in endpoint):
                raise ValueError('invalid SSH endpoint')
            storage.update(endpoint=endpoint.rstrip('/'), credential_prefix=kind.upper())
    identity = {'version': 2, 'profile': opts['profile'], 'name': request['name'], 'storage': storage}
    if kind != 'local':
        identity['object_key'] = key
    reference = json.dumps(identity, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return {'status': 'planned', 'directory': directory, 'object_key': key, 'storage': storage, 'reference': reference}


def _environment(source, requests):
    allowed = {'PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
               'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CA_BUNDLE',
               'GOOGLE_APPLICATION_CREDENTIALS', 'CLOUDSDK_CONFIG', 'COLORS_PAR_R2_ACCESS_KEY_ID',
               'COLORS_PAR_R2_SECRET_ACCESS_KEY', 'COLORS_PAR_OCI_ACCESS_KEY_ID', 'COLORS_PAR_OCI_SECRET_ACCESS_KEY'}
    for request in requests:
        allowed.update(request[k] for k in ('passphrase_env', 'new_passphrase_env') if k in request)
    return {k: v for k, v in source.items() if k in allowed and isinstance(v, str)}


def _process_closer(process, cleanup):
    """One independently shielded cleanup task, shared by all close callers."""
    closing = None

    async def finish():
        try:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), 5)
        finally:
            cleanup()

    async def close():
        nonlocal closing
        if closing is None:
            closing = asyncio.create_task(finish())
        cancellation = None
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as error:
                cancellation = error
        closing.result()
        if cancellation is not None:
            raise cancellation

    return close


async def _start(message, environment):
    directory = tempfile.TemporaryDirectory(prefix='colors-ssh-runtime-')
    path = Path(directory.name) / 'ssh-adapter'
    path.write_bytes(Path(__file__).with_name('ssh_adapter.py').read_bytes())
    path.chmod(0o700)
    try:
        process = await asyncio.create_subprocess_exec('python3', str(path), env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except BaseException:
        directory.cleanup()
        raise
    close = _process_closer(process, directory.cleanup)
    try:
        process.stdin.write((json.dumps(message) + '\n').encode())
        await process.stdin.drain()
    except BaseException:
        await close()
        raise
    return process, close


async def ssh_resource(opts, request, operation='create', environment=None):
    plan = ssh_plan(opts, request)
    if operation == 'build':
        return plan | {'status': 'built'}
    source = os.environ if environment is None else environment
    process, close = await _start({'plan': plan, 'request': request, 'operation': operation}, _environment(source, [request]))
    try:
        return json.loads(await asyncio.wait_for(process.stdout.readline(), 180))
    finally:
        await close()


async def start_agent(resources, environment, register, lifetime=900):
    entries = [{'plan': ssh_plan(entry['opts'], entry['request']), 'request': entry['request'], 'resource': entry['resource']} for entry in resources]
    process, close = await _start({'operation': 'agent', 'resources': entries, 'lifetime': lifetime},
                                  _environment(environment, [e['request'] for e in entries]))
    try:
        register('resource', close)
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 180))
        if ready.get('status') != 'ready':
            raise RuntimeError('SSH agent setup failed')
        return ready
    except BaseException:
        await close()
        raise
