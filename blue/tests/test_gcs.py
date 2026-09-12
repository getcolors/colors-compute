import json
from urllib.error import HTTPError
from urllib.parse import urlparse, parse_qs, unquote
import pytest
from colors_compute.backend import ProcessResult
from colors_compute.rendering import backend_plan
from colors_compute.journal import _settings, _identity
from colors_compute.coordination import _identity as identity_valid
from colors_compute.managed_backend import bootstrap_backend, finalize_backend
from colors_compute.gcs import gcs_client, gcs_put

OPTS = {'provider-backend': 'gcs', 'provider-compute': 'google', 'gcs-bucket': 'demo-states',
        'gcs-region': 'us-central1', 'google-project': 'demo-project', 'profile': 'demo',
        'gcs-bucket-mode': 'managed', 'compute-prevent-destroy': False}


def test_native_state_prefix_and_identity():
    assert backend_plan(OPTS, 'demo/compute/shared.tfstate')['config']['terraform']['backend'] == {
        'gcs': {'bucket': 'demo-states', 'prefix': 'demo/compute/shared.tfstate'}}
    assert identity_valid(_identity(OPTS, _settings(OPTS)))


class Owner:
    def __init__(self, phase='retired'):
        self.phase, self.released = phase, False
    async def acquire(self): pass
    async def snapshot(self): return {'document': {'status': self.phase}}
    async def release(self): self.released = True


class GCS:
    def __init__(self):
        self.metadata = None
        self.objects = {}
        self.deleted = []
        self.created = 0
        self.versions = []
    async def request(self, method, path, body=None, query=None):
        if path.startswith('v1/projects/'):
            return {'projectId': 'demo-project', 'projectNumber': '123456789'}
        query = query or {}
        name = unquote(path.split('/o/', 1)[1]) if '/o/' in path else None
        if method == 'POST' and path == 'storage/v1/b':
            self.created += 1
            self.metadata = {**body, 'metageneration': '1', 'projectNumber': '123456789'}
            return self.metadata
        if path.startswith('upload/'):
            self.objects[query['name']] = body
            return {'generation': '2'}
        if method == 'DELETE':
            self.deleted.append((name, query.get('generation')))
            return {}
        if method == 'PATCH':
            self.metadata.update(body)
            return self.metadata
        if path.endswith('/o'):
            return {'items': self.versions if query.get('versions') else [{'name': k} for k in self.objects]}
        if name:
            if name not in self.objects: return None
            return self.objects[name] if query.get('alt') == 'media' else {'generation': '1'}
        return self.metadata
    async def client(self, *args): return self.request


@pytest.mark.asyncio
async def test_managed_create_repeat_and_unowned_refusal(monkeypatch):
    api = GCS()
    monkeypatch.setattr('colors_compute.managed_gcs_backend.gcs_client', api.client)
    assert await bootstrap_backend(OPTS) == {'status': 'ready', 'bucket': 'demo-states'}
    api.metadata['versioning']['enabled'] = False
    api.metadata['iamConfiguration']['publicAccessPrevention'] = 'inherited'
    assert await bootstrap_backend(OPTS) == {'status': 'ready', 'bucket': 'demo-states'}
    assert api.created == 1
    assert api.metadata['versioning']['enabled'] is True
    assert api.metadata['iamConfiguration']['publicAccessPrevention'] == 'enforced'
    assert api.metadata['softDeletePolicy']['retentionDurationSeconds'] == '0'
    api.metadata['labels']['colors_profile'] = 'foreign'
    with pytest.raises(ValueError, match='ownership mismatch'):
        await bootstrap_backend(OPTS)


@pytest.mark.asyncio
async def test_retirement_guard_and_all_generations_cleanup(monkeypatch):
    api = GCS()
    monkeypatch.setattr('colors_compute.managed_gcs_backend.gcs_client', api.client)
    await bootstrap_backend(OPTS)
    owner = Owner('active')
    with pytest.raises(ValueError, match='compute must retire'):
        await finalize_backend(OPTS, coordinator_factory=lambda *a, **kw: owner)
    assert owner.released and not api.deleted
    owner = Owner()
    state = 'demo/compute/shared.tfstate/default.tfstate'
    api.objects[state] = {'version': 4, 'resources': []}
    api.versions = [{'name': state, 'generation': '1'}, {'name': state, 'generation': '2'},
                    {'name': '_colors/backend-owner.json', 'generation': '3'}]
    assert await finalize_backend(OPTS, coordinator_factory=lambda *a, **kw: owner) == {'status': 'destroyed'}
    assert api.deleted == [(state, '1'), (state, '2'), ('_colors/backend-owner.json', '3'), (None, None)]


@pytest.mark.asyncio
async def test_wire_generation_precondition_conflict(monkeypatch):
    async def runner(*args): return ProcessResult(0, 'test-token', '')
    def request(req, **kwargs):
        assert parse_qs(urlparse(req.full_url).query)['ifGenerationMatch'] == ['123']
        assert req.headers['Authorization'] == 'Bearer test-token'
        raise HTTPError(req.full_url, 412, 'conflict', {}, None)
    monkeypatch.setattr('colors_compute.gcs.urlopen', request)
    assert await gcs_put(await gcs_client({}, runner), 'demo-states', 'journal', {}, '123') == {'conflict': True}

@pytest.mark.asyncio
async def test_state_presence_uses_physical_key_and_refuses_foreign_key(monkeypatch):
    from colors_compute.execution import state_presence
    calls = []
    async def request(method, path):
        calls.append(path)
        return None
    async def client(*args): return request
    monkeypatch.setattr('colors_compute.gcs.gcs_client', client)
    assert await state_presence(OPTS, 'demo/compute/shared.tfstate') == {'status': 'absent'}
    assert calls == ['storage/v1/b/demo-states/o/demo%2Fcompute%2Fshared.tfstate%2Fdefault.tfstate']
    assert await state_presence(OPTS, 'foreign/compute/shared.tfstate') == {'status': 'error'}
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_bounded_generation_read_rejects_before_download():
    from colors_compute.gcs import gcs_get
    calls = []
    async def request(*args, **kwargs):
        calls.append(args)
        return {'generation': '1', 'size': '2097153'}
    with pytest.raises(ValueError, match='too large'):
        await gcs_get(request, 'states', 'journal', 2097152)
    assert len(calls) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['bootstrap', 'finalize'])
@pytest.mark.parametrize('number', ['987654321', None, 123456789])
async def test_matching_marker_cannot_authorize_foreign_project(monkeypatch, action, number):
    api = GCS()
    monkeypatch.setattr('colors_compute.managed_gcs_backend.gcs_client', api.client)
    await bootstrap_backend(OPTS)
    api.metadata['projectNumber'] = number
    calls = []
    original = api.request
    async def observed(method, *args, **kwargs):
        calls.append(method)
        return await original(method, *args, **kwargs)
    api.request = observed
    with pytest.raises(ValueError, match='ownership mismatch'):
        await (bootstrap_backend(OPTS) if action == 'bootstrap' else finalize_backend(OPTS))
    assert set(calls) == {'GET'}


@pytest.mark.asyncio
@pytest.mark.parametrize('resolved', [None, {}, {'projectId': 'foreign-project', 'projectNumber': '123456789'},
    {'projectId': 'demo-project', 'projectNumber': 123456789}, {'projectId': 'demo-project', 'projectNumber': '1.2'},
    {'projectId': 'demo-project', 'projectNumber': '0'}])
async def test_project_resolution_fails_before_bucket_access(monkeypatch, resolved):
    calls = []
    async def request(method, path, *args, **kwargs):
        calls.append((method, path))
        return resolved
    async def client(*args): return request
    monkeypatch.setattr('colors_compute.managed_gcs_backend.gcs_client', client)
    with pytest.raises(ValueError, match='project verification failed'):
        await bootstrap_backend(OPTS)
    assert calls == [('GET', 'v1/projects/demo-project')]


@pytest.mark.asyncio
@pytest.mark.parametrize('bucket_status', [200, 404, 403])
async def test_wire_object_404_requires_existing_readable_bucket(monkeypatch, bucket_status):
    async def runner(*args): return ProcessResult(0, 'test-token', '')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"name":"demo-states"}'
    def send(req, **kwargs):
        status = 404 if '/o/' in req.full_url else bucket_status
        if status != 200: raise HTTPError(req.full_url, status, 'error', {}, None)
        return Response()
    monkeypatch.setattr('colors_compute.gcs.urlopen', send)
    request = await gcs_client({}, runner)
    if bucket_status == 200:
        assert await request('GET', 'storage/v1/b/demo-states/o/missing') is None
    else:
        with pytest.raises(ValueError, match='bucket missing' if bucket_status == 404 else '403'):
            await request('GET', 'storage/v1/b/demo-states/o/missing')
    if bucket_status == 404:
        assert await request('GET', 'storage/v1/b/demo-states') is None


@pytest.mark.asyncio
async def test_project_lookup_uses_resource_manager_and_rejects_denial(monkeypatch):
    async def runner(*args): return ProcessResult(0, 'test-token', '')
    def send(req, **kwargs):
        assert req.full_url == 'https://cloudresourcemanager.googleapis.com/v1/projects/demo-project'
        assert req.headers['Authorization'] == 'Bearer test-token'
        raise HTTPError(req.full_url, 403, 'forbidden', {}, None)
    monkeypatch.setattr('colors_compute.gcs.urlopen', send)
    with pytest.raises(ValueError, match='403'):
        await bootstrap_backend(OPTS, environment={}, runner=runner)

@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'conflict': True}, {'name': 'foreign'}, []])
async def test_wire_object_404_rejects_unverified_bucket_metadata(monkeypatch, payload):
    async def runner(*args): return ProcessResult(0, 'test-token', '')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(payload).encode()
    def send(req, **kwargs):
        if '/o/' in req.full_url: raise HTTPError(req.full_url, 404, 'missing', {}, None)
        return Response()
    monkeypatch.setattr('colors_compute.gcs.urlopen', send)
    with pytest.raises(ValueError, match='invalid GCS bucket metadata'):
        await (await gcs_client({}, runner))('GET', 'storage/v1/b/demo-states/o/missing')


@pytest.mark.asyncio
async def test_created_bucket_project_must_match_before_marker_write(monkeypatch):
    api = GCS()
    original = api.request
    async def request(method, path, *args, **kwargs):
        result = await original(method, path, *args, **kwargs)
        if method == 'POST' and path == 'storage/v1/b': result['projectNumber'] = '987654321'
        return result
    api.request = request
    monkeypatch.setattr('colors_compute.managed_gcs_backend.gcs_client', api.client)
    with pytest.raises(ValueError, match='ownership mismatch'):
        await bootstrap_backend(OPTS)
    assert not api.objects
