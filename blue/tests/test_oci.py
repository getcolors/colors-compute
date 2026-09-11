import json
from urllib.parse import unquote
import pytest
from colors_compute.backend import ProcessResult
from colors_compute.rendering import backend_plan
from colors_compute.journal import _settings, _identity, journal_get
from colors_compute.coordination import _identity as identity_valid
from colors_compute.managed_backend import bootstrap_backend, finalize_backend
from colors_compute.oci import oci_client
from colors_compute.execution import state_presence

OPTS = {'oci-ocpus': 1, 'provider-backend': 'oci', 'provider-compute': 'oci', 'oci-bucket': 'demo-states',
        'oci-region': 'eu-frankfurt-1', 'oci-namespace': 'namespace1', 'oci-compartment-id': 'ocid1.compartment.example',
        'profile': 'demo', 'oci-bucket-mode': 'managed', 'compute-prevent-destroy': False}


def test_oci_state_plan_and_identity():
    plan = backend_plan(OPTS, 'demo/compute/shared.tfstate')
    backend = plan['config']['terraform']['backend']['s3']
    assert backend['endpoints']['s3'] == 'https://namespace1.compat.objectstorage.eu-frankfurt-1.oraclecloud.com'
    assert backend['use_path_style'] and backend['use_lockfile']
    assert plan['credential_bindings'] == {'COLORS_PAR_OCI_ACCESS_KEY_ID': 'access_key', 'COLORS_PAR_OCI_SECRET_ACCESS_KEY': 'secret_key'}
    assert identity_valid(_identity(OPTS, _settings(OPTS)))


class Owner:
    def __init__(self, phase='retired'): self.phase, self.released = phase, False
    async def acquire(self): pass
    async def snapshot(self): return {'document': {'status': self.phase}}
    async def release(self): self.released = True


class OCI:
    def __init__(self):
        self.metadata, self.objects, self.deleted, self.versions, self.created = None, {}, [], [], 0
        self.list_denied = False
    async def client(self, *a): return self.request
    async def request(self, method, path, body=None, query=None, headers=None):
        query, headers = query or {}, headers or {}
        key = unquote(path.split('/o/', 1)[1]) if '/o/' in path else None
        def result(data): return {'data': data, 'headers': {'etag': 'e1'}}
        if path.endswith('/b'):
            if method == 'GET': return None if self.list_denied else result([])
            self.created += 1; self.metadata = body.copy(); return result(self.metadata)
        if method == 'DELETE':
            self.deleted.append((key, query.get('versionId'))); return result(None)
        if key:
            if method == 'GET': return result(self.objects[key]) if key in self.objects else None
            assert headers.get('if-match') == 'e1' or headers.get('if-none-match') == '*'
            self.objects[key] = body; return result(None)
        if path.endswith('/o'): return result({'objects': [{'name': k} for k in self.objects]})
        if path.endswith('/objectversions'): return result({'items': self.versions})
        assert method in ('GET', 'POST'), 'OCI bucket updates require POST'
        if method == 'POST':
            assert headers.get('content-type') == 'application/json'
            self.metadata.update(body)
        return result(self.metadata) if self.metadata else None


@pytest.mark.asyncio
async def test_managed_create_repeat_and_refuse_foreign(monkeypatch):
    api = OCI(); monkeypatch.setattr('colors_compute.managed_oci_backend.oci_client', api.client)
    assert await bootstrap_backend(OPTS) == {'status': 'ready', 'bucket': 'demo-states'}
    api.metadata['versioning'] = 'Suspended'
    await bootstrap_backend(OPTS)
    assert api.created == 1 and api.metadata['versioning'] == 'Enabled'
    api.metadata['freeformTags']['colors-profile'] = 'foreign'
    with pytest.raises(ValueError, match='ownership mismatch'): await bootstrap_backend(OPTS)


@pytest.mark.asyncio
async def test_absence_requires_compartment_listing(monkeypatch):
    api = OCI(); api.list_denied = True
    monkeypatch.setattr('colors_compute.managed_oci_backend.oci_client', api.client)
    with pytest.raises(ValueError, match='absence unconfirmed'): await bootstrap_backend(OPTS)
    assert api.created == 0


@pytest.mark.asyncio
async def test_retirement_active_state_and_versions(monkeypatch):
    api = OCI(); monkeypatch.setattr('colors_compute.managed_oci_backend.oci_client', api.client)
    await bootstrap_backend(OPTS)
    owner = Owner('active')
    with pytest.raises(ValueError, match='compute must retire'): await finalize_backend(OPTS, coordinator_factory=lambda *a, **kw: owner)
    assert owner.released and not api.deleted
    state = 'demo/compute/shared.tfstate'; api.objects[state] = {'version': 4, 'resources': [{'instances': [{}]}]}
    with pytest.raises(ValueError, match='live state'): await finalize_backend(OPTS, coordinator_factory=lambda *a, **kw: Owner())
    assert not api.deleted
    api.objects[state]['resources'] = []
    api.versions = [{'name': '_colors/backend-owner.json', 'versionId': '3'}, {'name': state, 'versionId': '1'}, {'name': state, 'versionId': '2'}]
    assert await finalize_backend(OPTS, coordinator_factory=lambda *a, **kw: Owner()) == {'status': 'destroyed'}
    assert api.deleted == [(state, '1'), (state, '2'), ('_colors/backend-owner.json', '3'), (None, None)]
    assert api.metadata['freeformTags']['colors-phase'] == 'deleting'
    with pytest.raises(ValueError, match='deletion in progress'): await bootstrap_backend(OPTS)


@pytest.mark.asyncio
async def test_raw_request_preconditions_and_denial():
    async def runner(args, cwd, env, timeout):
        assert 'COLORS_PAR_OCI_SECRET_ACCESS_KEY' not in env
        assert json.loads(args[args.index('--request-headers')+1]) == {'if-match': 'etag'}
        assert args[args.index('--auth')+1] == 'security_token'
        return ProcessResult(0, json.dumps({'status': '412 Precondition Failed'}))
    request = await oci_client(OPTS, {'COLORS_PAR_OCI_SECRET_ACCESS_KEY': 'secret'}, runner)
    assert await request('PUT', '/n/test/b/test/o/key', {}, headers={'if-match': 'etag'}) == {'conflict': True}


@pytest.mark.asyncio
async def test_s3_transport_isolates_credentials_and_uses_oci_endpoint():
    from pathlib import Path
    async def runner(args, cwd, env, timeout):
        assert env.get('AWS_PROFILE') is None and env.get('AWS_ACCESS_KEY_ID') is None
        assert 'oci-key' in Path(env['AWS_SHARED_CREDENTIALS_FILE']).read_text()
        assert args[args.index('--endpoint-url')+1] == 'https://namespace1.compat.objectstorage.eu-frankfurt-1.oraclecloud.com'
        return ProcessResult(1, '', 'An error occurred (NoSuchKey) when calling the GetObject operation: absent')
    assert await state_presence(OPTS, 'demo/compute/shared.tfstate', {'AWS_PROFILE': 'foreign', 'AWS_ACCESS_KEY_ID': 'foreign', 'COLORS_PAR_OCI_ACCESS_KEY_ID': 'oci-key', 'COLORS_PAR_OCI_SECRET_ACCESS_KEY': 'oci-secret'}, runner) == {'status': 'absent'}

@pytest.mark.asyncio
async def test_journal_native_cas_rejects_stale_etag():
    from colors_compute.journal import journal_put
    from colors_compute.coordination import coordination
    intent = coordination({'status': 'absent'}, _identity(OPTS, _settings(OPTS)), {'type': 'acquire', 'run_id': 'run', 'write_id': 'first', 'target_etag': None})
    async def runner(args, cwd, env, timeout):
        assert args[:2] == ['oci', 'raw-request']
        headers = json.loads(args[args.index('--request-headers')+1])
        assert headers == {'if-match': 'stale', 'content-type': 'application/json'}
        return ProcessResult(0, json.dumps({'status': '412 Precondition Failed'}))
    intent['condition'] = {'if_match': 'stale'}
    assert await journal_put(OPTS, intent, {}, runner) == {'status': 'conflict'}

@pytest.mark.asyncio
async def test_version_pages_and_resume_old_marker_after_partial_purge(monkeypatch):
    api = OCI(); monkeypatch.setattr('colors_compute.managed_oci_backend.oci_client', api.client)
    await bootstrap_backend(OPTS)
    # A previous purge removed the latest deleting marker version. Its active
    # predecessor is visible, but the durable bucket tag authorizes resumption.
    api.metadata['freeformTags']['colors-phase'] = 'deleting'
    original = api.request
    async def request(method, path, body=None, query=None, headers=None):
        if path.endswith('/objectversions'):
            assert 'start' not in (query or {})
            if not query:
                return {'data': {'items': [{'name': 'demo/old.tfstate', 'versionId': '1'}]}, 'headers': {'opc-next-page': 'next'}}
            assert query == {'page': 'next'}
            return {'data': {'items': [{'name': '_colors/backend-owner.json', 'versionId': '2'}]}, 'headers': {}}
        return await original(method, path, body, query, headers)
    api.request = request
    def forbidden(*args, **kwargs): raise AssertionError('retired journal was already purged')
    assert await finalize_backend(OPTS, coordinator_factory=forbidden) == {'status': 'destroyed'}
    assert api.deleted == [('demo/old.tfstate', '1'), ('_colors/backend-owner.json', '2'), (None, None)]

@pytest.mark.parametrize('domains', [[], ['ad1', 'ad1'], ['${injected}'], 'ad1'])
def test_invalid_domain_vectors(domains):
    from colors_compute.oci import place_node
    with pytest.raises(ValueError, match='invalid OCI availability domains'):
        place_node({**OPTS, 'oci-availability-domains': domains}, 'node', '0')


def test_domain_selection_is_stable_and_shared_is_unchanged():
    from colors_compute.oci import place_node
    opts = {**OPTS, 'oci-availability-domain': 'legacy', 'oci-availability-domains': ['ad1', 'ad2', 'ad3']}
    assert place_node(opts, 'node', 'broker-2')['oci-availability-domain'] == 'ad3'
    assert place_node(opts, 'node', '3')['oci-availability-domain'] == 'ad1'
    assert place_node(opts, 'shared', 'shared') == opts
    assert opts['oci-availability-domain'] == 'legacy'
