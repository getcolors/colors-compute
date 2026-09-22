import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import time
import pytest
from colors_compute.ssh import ssh_plan, ssh_resource, start_agent
from colors_compute import ssh_adapter as adapter


@pytest.fixture
def setup(tmp_path, monkeypatch):
    opts = {'profile': 'alice-test', 'provider-backend': 'local'}
    request = {'name': 'app-access', 'workdir': str(tmp_path.resolve()), 'passphrase_env': 'COLORS_PAR_TEST_SSH'}
    monkeypatch.setenv('COLORS_PAR_TEST_SSH', 'space ☃ $`"\\ unusual passphrase')
    monkeypatch.setenv('COLORS_PAR_TEST_NEW', 'different ☃ passphrase')
    return opts, request, ssh_plan(opts, request)


def test_plan_backend_identity_and_isolation(setup):
    opts, request, local = setup
    assert local['directory'].endswith('/alice-test/ssh/app-access')
    assert local['storage']['path'].endswith('/resource.json')
    remote = ssh_plan(opts, dict(request, backend={'provider-backend':'r2', 'r2-bucket':'alice-state', 'r2-endpoint':'https://account.r2.cloudflarestorage.com', 's3-prefix':'sdk'}))
    assert remote['object_key'] == 'sdk/alice-test/ssh/app-access/resource.json'
    assert request['workdir'] not in remote['reference']
    assert remote['reference'] == ssh_plan(opts, dict(request, workdir='/different', backend={'provider-backend':'r2', 'r2-bucket':'alice-state', 'r2-endpoint':'https://account.r2.cloudflarestorage.com', 's3-prefix':'sdk'}))['reference']
    assert local['reference'] != ssh_plan(opts, dict(request, name='database-access'))['reference']
    with pytest.raises(ValueError): ssh_plan(opts, dict(request, name='../escape'))
    with pytest.raises(ValueError): ssh_plan(opts, dict(request, backend={'profile':'other'}))


def test_real_generation_reuse_and_no_plaintext(setup):
    _, request, plan = setup
    first = adapter.resource(plan, request, 'create')
    second = adapter.resource(plan, request, 'create')
    assert first == second
    text = (Path(plan['directory']) / 'resource.json').read_text()
    record = json.loads(text)
    assert record['kdf_rounds'] == 64
    assert adapter.encrypted_public(record['encrypted_key']) == first['public_key']
    assert os.environ['COLORS_PAR_TEST_SSH'] not in text
    assert not any('private' in k or 'encrypted' in k for k in first)
    assert Path(plan['directory']).stat().st_mode & 0o777 == 0o700
    assert (Path(plan['directory']) / 'resource.json').stat().st_mode & 0o777 == 0o600


def test_rotation_preserves_identity_and_wrong_secret_does_not_poison(setup, monkeypatch):
    _, request, plan = setup
    first = adapter.resource(plan, request, 'create')
    before = json.loads((Path(plan['directory']) / 'resource.json').read_text())['encrypted_key']
    rotated = adapter.resource(plan, dict(request, new_passphrase_env='COLORS_PAR_TEST_NEW'), 'rotate')
    assert rotated == first
    record = json.loads((Path(plan['directory']) / 'resource.json').read_text())
    assert record['encrypted_key'] != before
    with pytest.raises(adapter.ResourceError):
        adapter.verify_unlock(record, request['passphrase_env'], Path(plan['directory']))
    adapter.verify_unlock(record, 'COLORS_PAR_TEST_NEW', Path(plan['directory']))
    with pytest.raises(adapter.ResourceError):
        adapter.resource(plan, dict(request, new_passphrase_env='COLORS_PAR_TEST_NEW'), 'rotate')
    assert adapter.resource(plan, request, 'inspect') == first


def test_missing_authority_and_delete_tombstone(setup):
    _, request, plan = setup
    result = adapter.resource(plan, request, 'create')
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, request, 'delete')
    assert adapter.resource(plan, dict(request, allow_delete=True, consumers_destroyed=True), 'delete')['status'] == 'destroyed'
    assert adapter.resource(plan, dict(request, allow_delete=True, consumers_destroyed=True), 'delete')['status'] == 'destroyed'
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, request, 'create')
    text = (Path(plan['directory']) / 'resource.json').read_text()
    assert 'encrypted_key' not in text
    (Path(plan['directory']) / 'resource.json').unlink()
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, dict(request, expected=result), 'create')


def test_interruption_reservation_and_explicit_recovery(setup):
    _, request, plan = setup
    with adapter.locked(plan, allow_absent=True): pass
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, request, 'create')
    record = json.loads((Path(plan['directory']) / 'resource.json').read_text())
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, dict(request, lock_token=record['lock_token']), 'recover')


def test_recovery_with_bundle_preserves_public_key(setup):
    _, request, plan = setup
    original = adapter.resource(plan, request, 'create')
    with adapter.locked(plan) as (_, record, _, _): token = record['lock_token']
    recovered = adapter.resource(plan, dict(request, lock_token=token), 'recover')
    assert recovered == original


def test_concurrent_create_fails_busy_and_then_reuses(setup):
    _, request, plan = setup
    with adapter.locked(plan, allow_absent=True):
        with pytest.raises(adapter.ResourceError, match='busy'): adapter.resource(plan, request, 'create')


def test_symlink_and_unsafe_file_refused(setup, tmp_path):
    _, request, plan = setup
    directory = Path(plan['directory'])
    directory.mkdir(parents=True)
    unrelated = tmp_path / 'unrelated'
    unrelated.write_text('not ours')
    (directory / 'resource.json').symlink_to(unrelated)
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, request, 'create')
    assert unrelated.read_text() == 'not ours'


def test_missing_secret_before_reservation(setup, monkeypatch):
    _, request, plan = setup
    monkeypatch.delenv('COLORS_PAR_TEST_SSH')
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, request, 'create')
    assert not Path(plan['directory']).exists()


@pytest.mark.asyncio
async def test_native_api_multi_key_agent_cleanup_and_secret_reuse(setup, monkeypatch):
    opts, request, plan = setup
    second_request = dict(request, name='database-access', passphrase_env='COLORS_PAR_TEST_NEW')
    first = await ssh_resource(opts, request)
    second = await ssh_resource(opts, second_request)
    cleanups = []
    agent = await start_agent([{'opts':opts, 'request':request, 'resource':first}, {'opts':opts, 'request':second_request, 'resource':second}], dict(os.environ), lambda phase, close: cleanups.append(close))
    socket = agent['socket']
    try:
        assert len(agent['identities']) == 2
        for public in agent['identities'].values():
            assert Path(public).read_text().startswith('ssh-ed25519 ')
        output = subprocess.check_output(['ssh-add','-l'], env={**os.environ,'SSH_AUTH_SOCK':socket}).decode()
        assert first['fingerprint'] in output and second['fingerprint'] in output
        assert 'SSH_AUTH_SOCK' not in agent['identities']
    finally:
        for close in cleanups: await close()
    assert not Path(socket).exists()
    assert (Path(plan['directory']) / 'resource.json').exists()


@pytest.mark.asyncio
async def test_failed_agent_load_registers_cleanup_and_releases_resource(setup, monkeypatch):
    opts, request, plan = setup
    result = await ssh_resource(opts, request)
    monkeypatch.setenv('COLORS_PAR_TEST_SSH', 'wrong')
    cleanups = []
    with pytest.raises(RuntimeError): await start_agent([{'opts':opts, 'request':request, 'resource':result}],dict(os.environ),lambda phase, close:cleanups.append(close))
    assert len(cleanups) == 1
    await cleanups[0]()
    assert (await ssh_resource(opts, request, 'inspect')) == result


def test_remote_conditional_writes_never_fallback(setup, monkeypatch):
    opts, request, _ = setup
    plan = ssh_plan(opts, dict(request, backend={'provider-backend':'r2', 'r2-bucket':'alice-state', 'r2-endpoint':'https://account.r2.cloudflarestorage.com'}))
    monkeypatch.setenv('COLORS_PAR_R2_ACCESS_KEY_ID', 'key')
    monkeypatch.setenv('COLORS_PAR_R2_SECRET_ACCESS_KEY', 'secret')
    store = adapter.Store(plan)
    calls = []
    def aws(verb, args):
        calls.append((verb,args))
        return 1, b'', b'access denied'
    store.aws = aws
    with pytest.raises(adapter.ResourceError): store.read()
    with pytest.raises(adapter.ResourceError): store.write({'status':'locked'}, None)
    assert '--if-none-match' in calls[-1][1]
    with pytest.raises(adapter.ResourceError): store.write({'status':'locked'}, 'etag')
    assert '--if-match' in calls[-1][1]
    assert len(calls) == 3

@pytest.mark.asyncio
async def test_agent_renews_only_while_scope_is_open(setup):
    opts, request, _ = setup
    result = await ssh_resource(opts, request)
    cleanups = []
    agent = await start_agent([{'opts':opts, 'request':request, 'resource':result}],dict(os.environ),lambda phase,close:cleanups.append(close), lifetime=4)
    try:
        await asyncio.sleep(6)
        output = subprocess.check_output(['ssh-add','-l'], env={**os.environ,'SSH_AUTH_SOCK':agent['socket']}).decode()
        assert result['fingerprint'] in output
    finally:
        await cleanups[0]()
    assert not Path(agent['socket']).exists()


def test_controlled_keygen_does_not_depend_on_askpass(setup, monkeypatch):
    _, request, plan = setup
    monkeypatch.setenv('SSH_ASKPASS', '/nonexistent')
    monkeypatch.setenv('SSH_ASKPASS_REQUIRE', 'force')
    generated = adapter.resource(plan, request, 'create')
    record = json.loads((Path(plan['directory']) / 'resource.json').read_text())
    assert adapter.encrypted_public(record['encrypted_key']) == generated['public_key']


def test_tampered_fingerprint_fails_before_reuse(setup):
    _, request, plan = setup
    adapter.resource(plan, request, 'create')
    path = Path(plan['directory']) / 'resource.json'
    record = json.loads(path.read_text())
    record['fingerprint'] = 'SHA256:wrong'
    path.write_text(json.dumps(record))
    with pytest.raises(adapter.ResourceError): adapter.resource(plan, request, 'create')


def test_missing_local_authority_never_regenerates_without_expected(setup):
    _, request, plan = setup
    adapter.resource(plan, request, 'create')
    (Path(plan['directory']) / 'resource.json').unlink()
    with pytest.raises(adapter.ResourceError, match='authority missing'):
        adapter.resource(plan, request, 'create')
    assert not (Path(plan['directory']) / 'resource.json').exists()


@pytest.mark.parametrize('value', ['\x15','\x04','tab\tsecret','\x7f','a'*1001,'☃'*334])
def test_terminal_controls_and_oversized_passphrases_fail_before_mutation(setup,monkeypatch,value):
    _,request,plan=setup
    monkeypatch.setenv('COLORS_PAR_TEST_SSH',value)
    with pytest.raises(adapter.ResourceError):adapter.resource(plan,request,'create')
    assert not Path(plan['directory']).exists()


def test_backend_overrides_have_distinct_public_caches(setup):
    opts,request,local=setup
    remote=ssh_plan(opts,dict(request,backend={'provider-backend':'r2','r2-bucket':'alice-state','r2-endpoint':'https://account.r2.cloudflarestorage.com'}))
    assert local['directory']==remote['directory']
    assert adapter.public_cache(local)!=adapter.public_cache(remote)
