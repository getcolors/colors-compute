"""Managed GCS state bucket ownership and complete generation cleanup."""
import re
from .gcs import gcs_client, gcs_get, gcs_put, bucket_path, object_path
from .coordinator import Coordinator

MARKER = '_colors/backend-owner.json'


async def managed_gcs_backend(opts, action, environment=None, runner=None, coordinator_factory=None):
    mode = opts.get('gcs-bucket-mode', 'external')
    if mode not in ('external', 'managed'):
        raise ValueError('invalid GCS bucket mode')
    if mode == 'external' or any(opts.get(f'{c}/dry-run') is True or opts.get(f'{c}/event') == 'build' for c in ('blue', 'red', 'green')):
        return {'status': 'skipped'}
    bucket, region, project, profile = (opts.get(k) for k in ('gcs-bucket', 'gcs-region', 'google-project', 'profile'))
    for value, pattern in ((bucket, r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]'), (region, r'[a-z][a-z0-9-]+'),
                           (project, r'[a-z][a-z0-9-]+'), (profile, r'[a-z0-9][a-z0-9_-]{0,62}')):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError('invalid managed GCS identity')
    request = await gcs_client(environment, runner)
    path = bucket_path(bucket)
    resolved = await request('GET', 'v1/projects/' + project)
    if not isinstance(resolved, dict) or resolved.get('projectId') != project or not isinstance(resolved.get('projectNumber'), str) or not re.fullmatch(r'[1-9][0-9]*', resolved['projectNumber']):
        raise ValueError('managed backend project verification failed')
    project_number = resolved['projectNumber']
    identity = dict(project=project, bucket=bucket, region=region, profile=profile)
    labels = dict(colors_profile=profile, colors_project=project, colors_purpose='managed-backend')
    metadata = await request('GET', path)
    owner = None
    deleting = False
    try:
        if action == 'presence':
            return {'status': 'absent' if metadata is None else 'present'}
        if metadata is None:
            if action == 'finalize' or any(opts.get(f'{c}/event') == 'delete' for c in ('blue', 'red', 'green')):
                return {'status': 'absent'}
            if opts.get('compute-require-existing-state') is True:
                raise ValueError('existing managed backend required')
            metadata = await request('POST', 'storage/v1/b', dict(name=bucket, location=region, labels=labels,
                iamConfiguration={'uniformBucketLevelAccess': {'enabled': True}, 'publicAccessPrevention': 'enforced'},
                versioning={'enabled': True}, softDeletePolicy={'retentionDurationSeconds': '0'}), {'project': project})
            if metadata.get('projectNumber') != project_number:
                raise ValueError('managed backend ownership mismatch')
            written = await gcs_put(request, bucket, MARKER, dict(schema=1, identity=identity, status='active'), '0')
            if written.get('conflict'):
                raise ValueError('managed backend ownership conflict')
        if metadata.get('projectNumber') != project_number or any(metadata.get('labels', {}).get(k) != v for k, v in labels.items()) or metadata.get('location', '').lower() != region:
            raise ValueError('managed backend ownership mismatch')
        observed = await gcs_get(request, bucket, MARKER)
        marker = observed['document'] if observed else None
        if marker is None and action == 'finalize' and metadata['labels'].get('colors_phase') == 'deleting':
            marker = dict(schema=1, identity=identity, status='deleting')
        if not isinstance(marker, dict) or marker.get('schema') != 1 or marker.get('identity') != identity or marker.get('status') not in ('active', 'deleting'):
            raise ValueError('managed backend ownership mismatch')
        if action == 'bootstrap':
            if marker['status'] != 'active' or metadata['labels'].get('colors_phase') == 'deleting':
                raise ValueError('managed backend deletion in progress')
            protected = await request('PATCH', path, {'iamConfiguration': {'uniformBucketLevelAccess': {'enabled': True}, 'publicAccessPrevention': 'enforced'},
                'versioning': {'enabled': True}, 'softDeletePolicy': {'retentionDurationSeconds': '0'}}, {'ifMetagenerationMatch': metadata['metageneration']})
            if protected and protected.get('conflict'):
                raise ValueError('managed backend metadata conflict')
            return {'status': 'ready', 'bucket': bucket}
        if opts.get('compute-prevent-destroy') is not False:
            raise ValueError('managed backend deletion protected')

        async def listing(versions=False):
            items, page_token = [], None
            while True:
                query = {'versions': True} if versions else {}
                if page_token:
                    query['pageToken'] = page_token
                page = await request('GET', path + '/o', query=query)
                items.extend(page.get('items', []))
                page_token = page.get('nextPageToken')
                if not page_token:
                    return items

        if marker['status'] == 'active':
            owner = (coordinator_factory or Coordinator)(opts, environment, event_prefix='lifecycle/')
            await owner.acquire()
            if (await owner.snapshot())['document'].get('status') != 'retired':
                raise ValueError('compute must retire before backend deletion')
            for item in await listing():
                name = item['name']
                if name in (MARKER, profile + '/compute/coordination.json'):
                    continue
                if not name.startswith(profile + '/') or not name.endswith('.tfstate/default.tfstate'):
                    raise ValueError('managed backend contains unexpected objects')
                state = (await gcs_get(request, bucket, name))['document']
                if state.get('version') != 4 or not isinstance(state.get('resources'), list) or any(r.get('instances') for r in state['resources']):
                    raise ValueError('managed backend contains live state')
            result = await gcs_put(request, bucket, MARKER, {**marker, 'status': 'deleting'}, observed['etag'])
            if result.get('conflict'):
                raise ValueError('managed backend ownership conflict')
            deleting = True
        tagged = await request('PATCH', path, {'labels': {**metadata['labels'], 'colors_phase': 'deleting'}},
                               {'ifMetagenerationMatch': metadata['metageneration']})
        if tagged.get('conflict'):
            raise ValueError('managed backend metadata conflict')
        for item in sorted(await listing(True), key=lambda x: x['name'] == MARKER):
            result = await request('DELETE', object_path(bucket, item['name']), query={
                'generation': item['generation'], 'ifGenerationMatch': item['generation']})
            if result and result.get('conflict'):
                raise ValueError('managed backend generation conflict')
        await request('DELETE', path)
        return {'status': 'destroyed'}
    finally:
        if owner is not None and not deleting:
            await owner.release()
