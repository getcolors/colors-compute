"""Explicitly owned S3 backend bootstrap and post-deployment disposal."""
import json
import os
import re
import tempfile
from pathlib import Path

from .backend import _run
from .coordinator import Coordinator

MARKER = '_colors/backend-owner.json'


async def _lifecycle(opts, action, environment=None, runner=None, coordinator_factory=None):
    if opts.get('provider-backend') == 'oci':
        from .managed_oci_backend import managed_oci_backend
        return await managed_oci_backend(opts, action, environment, runner, coordinator_factory)
    if opts.get('provider-backend') == 'gcs':
        from .managed_gcs_backend import managed_gcs_backend
        return await managed_gcs_backend(opts, action, environment, runner, coordinator_factory)
    mode = opts.get('s3-bucket-mode', 'external')
    if mode not in ('external', 'managed'):
        raise ValueError('invalid S3 bucket mode')
    if mode == 'external':
        return {'status': 'skipped'}
    if opts.get('provider-backend') != 's3':
        raise ValueError('managed backend requires S3')
    if any(opts.get(f'{color}/dry-run') is True or opts.get(f'{color}/event') == 'build' for color in ('blue', 'red', 'green')):
        return {'status': 'skipped'}
    bucket, region, profile = (opts.get(k) for k in ('s3-bucket', 's3-region', 'profile'))
    if not (isinstance(bucket, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket)
            and isinstance(region, str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', region)
            and isinstance(profile, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}', profile)):
        raise ValueError('invalid managed backend identity')
    env = {k: v for k, v in dict(os.environ if environment is None else environment).items()
           if not k.startswith(('COLORS_PAR_', 'TF_', 'TOFU_'))}
    env.update(AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off', AWS_MAX_ATTEMPTS='1')
    owner = None
    deleting = False
    with tempfile.TemporaryDirectory(prefix='colors-backend-') as directory:
        os.chmod(directory, 0o700)
        path = Path(directory)

        async def command(service, operation, *args, missing=()):
            result = await (runner or _run)(['aws', service, operation, *args, '--region', region,
                '--output', 'json', '--no-cli-pager'], directory, env, 120000)
            if result.exit:
                # Only an explicit service missing response permits creation.
                match = re.search(r'An error occurred \(([^()]+)\) when calling the ', result.err)
                if match and match[1] in missing:
                    return None
                raise ValueError('managed backend AWS operation failed')
            return json.loads(result.out) if result.out.strip() else {}

        account = (await command('sts', 'get-caller-identity'))['Account']
        if not re.fullmatch(r'\d{12}', account):
            raise ValueError('invalid AWS account')
        identity = {'account': account, 'bucket': bucket, 'region': region, 'profile': profile}
        base = ['--bucket', bucket, '--expected-bucket-owner', account]

        async def s3(operation, *args, **kwargs):
            return await command('s3api', operation, *base, *args, **kwargs)

        async def get(key, missing=()):
            target = path / 'read.json'
            result = await s3('get-object', '--key', key, str(target), missing=missing)
            if result is None:
                return None, None
            return json.loads(target.read_text()), result['ETag']

        async def put(document, etag=None):
            target = path / 'write.json'
            target.write_text(json.dumps(document))
            target.chmod(0o600)
            return await s3('put-object', '--key', MARKER, '--body', str(target),
                '--content-type', 'application/json', *(['--if-match', etag] if etag else ['--if-none-match', '*']))

        present = await s3('head-bucket', missing=('404', 'NoSuchBucket', 'NotFound'))
        if action == 'presence':
            return {'status': 'absent' if present is None else 'present'}
        if present is None:
            if action == 'finalize' or any(opts.get(f'{c}/event') == 'delete' for c in ('blue', 'red', 'green')):
                return {'status': 'absent'}
            if opts.get('compute-require-existing-state') is True:
                raise ValueError('existing managed backend required')
            args = ['--bucket', bucket, '--object-ownership', 'BucketOwnerEnforced']
            if region != 'us-east-1':
                args += ['--create-bucket-configuration', json.dumps({'LocationConstraint': region})]
            await command('s3api', 'create-bucket', *args)
            await s3('put-bucket-tagging', '--tagging', json.dumps({'TagSet': [
                {'Key': 'colors:profile', 'Value': profile}, {'Key': 'colors:owner', 'Value': account},
                {'Key': 'colors:purpose', 'Value': 'managed-backend'}]}))
            await put({'schema': 1, 'identity': identity, 'status': 'active'})
        tags = {t['Key']: t['Value'] for t in (await s3('get-bucket-tagging'))['TagSet']}
        if any(tags.get(k) != v for k, v in {'colors:profile': profile, 'colors:owner': account,
                                            'colors:purpose': 'managed-backend'}.items()):
            raise ValueError('managed backend ownership mismatch')
        location = (await s3('get-bucket-location')).get('LocationConstraint') or 'us-east-1'
        if location != region:
            raise ValueError('managed backend region mismatch')
        marker, etag = await get(MARKER, ('NoSuchKey',))
        if marker is None and action == 'finalize' and tags.get('colors:phase') == 'deleting':
            marker = {'schema': 1, 'identity': identity, 'status': 'deleting'}
        if not isinstance(marker, dict) or marker.get('schema') != 1 or marker.get('identity') != identity or marker.get('status') not in ('active', 'deleting'):
            raise ValueError('managed backend ownership mismatch')
        if action == 'bootstrap':
            if marker['status'] != 'active' or tags.get('colors:phase') == 'deleting':
                raise ValueError('managed backend deletion in progress')
            await s3('put-public-access-block', '--public-access-block-configuration', json.dumps({
                'BlockPublicAcls': True, 'IgnorePublicAcls': True, 'BlockPublicPolicy': True, 'RestrictPublicBuckets': True}))
            await s3('put-bucket-encryption', '--server-side-encryption-configuration', json.dumps({
                'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}))
            await s3('put-bucket-versioning', '--versioning-configuration', '{"Status":"Enabled"}')
            return {'status': 'ready', 'bucket': bucket}
        if opts.get('compute-prevent-destroy') is not False:
            raise ValueError('managed backend deletion protected')
        try:
            if marker['status'] == 'active':
                owner = (coordinator_factory or Coordinator)(opts, env, event_prefix='lifecycle/')
                await owner.acquire()
                doc = (await owner.snapshot())['document']
                if doc.get('status') != 'retired':
                    raise ValueError('compute must retire before backend deletion')
                # Refuse foreign objects, locks, active state, and unreadable state.
                objects = (await s3('list-objects-v2')).get('Contents', [])
                for item in objects:
                    key = item['Key']
                    if key == MARKER or key == profile + '/compute/coordination.json':
                        continue
                    if not key.startswith(profile + '/') or not key.endswith('.tfstate'):
                        raise ValueError('managed backend contains unexpected objects')
                    state, _ = await get(key)
                    if state.get('version') != 4 or not isinstance(state.get('resources'), list) or any(
                            r.get('instances') for r in state['resources']):
                        raise ValueError('managed backend contains live state')
                await put({**marker, 'status': 'deleting'}, etag)
                deleting = True
            await s3('put-bucket-tagging', '--tagging', json.dumps({'TagSet': [
                {'Key': k, 'Value': v} for k, v in {**tags, 'colors:phase': 'deleting'}.items()]}))
            # Deleting marker is durable proof that full retirement was checked.
            # Bootstrap refuses it, allowing an interrupted purge to resume.
            while True:
                versions = await s3('list-object-versions')
                entries = [*versions.get('Versions', []), *versions.get('DeleteMarkers', [])]
                others = [x for x in entries if x['Key'] != MARKER]
                batch = (others or entries)[:1000]
                if not batch:
                    break
                result = await s3('delete-objects', '--delete', json.dumps({'Objects': [
                    {'Key': x['Key'], 'VersionId': x['VersionId']} for x in batch], 'Quiet': True}))
                if result.get('Errors'):
                    raise ValueError('managed backend version deletion failed')
            await s3('delete-bucket')
            return {'status': 'destroyed'}
        finally:
            if owner is not None and not deleting:
                await owner.release()


async def bootstrap_backend(opts, environment=None, runner=None):
    return await _lifecycle(opts, 'bootstrap', environment, runner)


async def backend_presence(opts, environment=None, runner=None):
    """Read-only: does the managed bucket exist? skipped for external mode, build and dry-run."""
    return await _lifecycle(opts, 'presence', environment, runner)


async def finalize_backend(opts, environment=None, runner=None, coordinator_factory=None):
    return await _lifecycle(opts, 'finalize', environment, runner, coordinator_factory)
