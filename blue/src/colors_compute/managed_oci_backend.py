"""Deployment-owned OCI backend buckets, with retired-state checks before purge."""
import re
from .oci import oci_client, bucket_path, object_path

MARKER = '_colors/backend-owner.json'


def check(value, message):
    if not value:
        raise ValueError(message)


async def managed_oci_backend(opts, action, environment=None, runner=None, coordinator_factory=None):
    mode = opts.get('oci-bucket-mode', 'external')
    check(mode in ('external', 'managed'), 'invalid OCI bucket mode')
    if mode == 'external' or any(opts.get(c+'/dry-run') is True or opts.get(c+'/event') == 'build' for c in ('blue', 'red', 'green')):
        return {'status': 'skipped'}
    bucket, region, namespace, compartment, profile = [opts.get(k) for k in ('oci-bucket', 'oci-region', 'oci-namespace', 'oci-compartment-id', 'profile')]
    check(all(isinstance(v, str) for v in (bucket, region, namespace, compartment, profile)) and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket) and re.fullmatch(r'[a-z][a-z0-9-]+', region) and re.fullmatch(r'[a-zA-Z0-9]+', namespace) and compartment.startswith('ocid1.') and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', profile), 'invalid managed OCI identity')
    request = await oci_client(opts, environment, runner)
    path = bucket_path(opts)
    identity = dict(bucket=bucket, region=region, namespace=namespace, compartment=compartment, profile=profile)
    tags = {'colors-profile': profile, 'colors-purpose': 'managed-backend'}
    observed = await request('GET', path)
    owner, deleting = None, False
    try:
        if observed is None:
            # OCI deliberately conflates denied and absent. A complete compartment
            # listing must confirm absence before create or an idempotent delete.
            page = None
            while True:
                listed = await request('GET', path.rsplit('/', 1)[0], query={'compartmentId': compartment, **({'page': page} if page else {})})
                check(listed is not None and 'conflict' not in listed and isinstance(listed['data'], list), 'OCI bucket absence unconfirmed')
                check(not any(b['name'] == bucket for b in listed['data']), 'OCI bucket absence unconfirmed')
                page = listed['headers'].get('opc-next-page')
                if not page:
                    break
            if action == 'finalize' or any(opts.get(c+'/event') == 'delete' for c in ('blue', 'red', 'green')):
                return {'status': 'absent'}
            check(opts.get('compute-require-existing-state') is not True, 'existing managed backend required')
            observed = await request('POST', path.rsplit('/', 1)[0], dict(name=bucket, compartmentId=compartment, freeformTags=tags, publicAccessType='NoPublicAccess', versioning='Enabled'))
            check(observed and not observed.get('conflict'), 'managed backend creation conflict')
            written = await request('PUT', object_path(opts, MARKER), dict(schema=1, identity=identity, status='active'), headers={'if-none-match': '*', 'content-type': 'application/json'})
            check(written and not written.get('conflict'), 'managed backend ownership conflict')
        metadata = observed['data']
        check(metadata.get('compartmentId') == compartment and all(metadata.get('freeformTags', {}).get(k) == v for k, v in tags.items()), 'managed backend ownership mismatch')
        marker_response = await request('GET', object_path(opts, MARKER))
        marker = marker_response['data'] if marker_response else None
        if marker is None and action == 'finalize' and metadata['freeformTags'].get('colors-phase') == 'deleting':
            marker = dict(schema=1, identity=identity, status='deleting')
        check(isinstance(marker, dict) and marker.get('schema') == 1 and marker.get('identity') == identity and marker.get('status') in ('active', 'deleting'), 'managed backend ownership mismatch')
        if action == 'finalize' and metadata['freeformTags'].get('colors-phase') == 'deleting':
            marker = {**marker, 'status': 'deleting'}
        async def update(body):
            updated = await request('PUT', path, body, headers={'if-match': observed['headers']['etag']})
            check(updated and not updated.get('conflict'), 'managed backend metadata conflict')
        if action == 'bootstrap':
            check(marker['status'] == 'active' and metadata['freeformTags'].get('colors-phase') != 'deleting', 'managed backend deletion in progress')
            if metadata.get('versioning') != 'Enabled' or metadata.get('publicAccessType') != 'NoPublicAccess':
                await update(dict(versioning='Enabled', publicAccessType='NoPublicAccess'))
            return {'status': 'ready', 'bucket': bucket}
        check(opts.get('compute-prevent-destroy') is False, 'managed backend deletion protected')
        async def listing(versions=False):
            items, start = [], None
            while True:
                response = await request('GET', path + ('/objectversions' if versions else '/o'), query={('page' if versions else 'start'): start} if start else {})
                check(response and not response.get('conflict'), 'managed backend listing failed')
                page = response['data']
                check(isinstance(page.get('items' if versions else 'objects'), list), 'managed backend listing malformed')
                items += page['items' if versions else 'objects']
                start = response['headers'].get('opc-next-page') if versions else page.get('nextStartWith')
                if not start:
                    return items
        if marker['status'] == 'active':
            if coordinator_factory:
                owner = coordinator_factory(opts, environment=environment, event_prefix='lifecycle/')
            else:
                from .coordinator import Coordinator
                owner = Coordinator(opts, environment=environment, event_prefix='lifecycle/')
            await owner.acquire()
            check((await owner.snapshot())['document']['status'] == 'retired', 'compute must retire before backend deletion')
            for item in await listing():
                key = item['name']
                if key in (MARKER, profile+'/compute/coordination.json'):
                    continue
                check(key.startswith(profile+'/') and key.endswith('.tfstate'), 'managed backend contains unexpected objects')
                state = (await request('GET', object_path(opts, key)))['data']
                check(state.get('version') == 4 and isinstance(state.get('resources'), list) and not any(r.get('instances') for r in state['resources']), 'managed backend contains live state')
            written = await request('PUT', object_path(opts, MARKER), {**marker, 'status': 'deleting'}, headers={'if-match': marker_response['headers']['etag'], 'content-type': 'application/json'})
            check(written and not written.get('conflict'), 'managed backend ownership conflict')
            deleting = True
        await update({'freeformTags': {**metadata['freeformTags'], 'colors-phase': 'deleting'}})
        for item in sorted(await listing(True), key=lambda i: i['name'] == MARKER):
            check(item.get('versionId'), 'managed backend version missing')
            deleted = await request('DELETE', object_path(opts, item['name']), query={'versionId': item['versionId']})
            check(deleted is None or not deleted.get('conflict'), 'managed backend version conflict')
        deleted = await request('DELETE', path)
        check(deleted is None or not deleted.get('conflict'), 'managed backend bucket deletion conflict')
        return {'status': 'destroyed'}
    finally:
        if owner and not deleting:
            await owner.release()
