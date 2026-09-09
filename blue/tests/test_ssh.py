import asyncio
import os
from pathlib import Path
import stat

import pytest

from colors_compute.ssh import prepare_keypair, cleanup_keypair, SSHError

OPTS = {'profile': 'demo', 'provider-compute': 'vultr'}


def environment(home):
    return {'HOME': str(home), 'PATH': os.environ['PATH']}


@pytest.mark.asyncio
async def test_real_key_is_shared_verified_and_removed_last(tmp_path):
    records = []
    def intent():
        records.append('intent')
        assert not (tmp_path / '.ssh/demo').exists()
        return True
    def prepared(fingerprint):
        records.append(fingerprint)
        return True
    env = environment(tmp_path)
    key = await prepare_keypair(OPTS, {'status': 'fresh'}, env, intent, prepared)
    assert records == ['intent', key['fingerprint']]
    assert key['public_key'].endswith('demo managed by Colors')
    assert 'PRIVATE KEY' not in repr(key)
    path = Path(key['private_key_path'])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    owned = {'status': 'prepared', 'fingerprint': key['fingerprint']}
    reused = await prepare_keypair(OPTS, owned, env, lambda: pytest.fail('no new intent'), lambda *_: pytest.fail('no new key'))
    assert reused == key
    with pytest.raises(SSHError, match='complete resource destruction'):
        await cleanup_keypair(OPTS, owned, {'all_resources_destroyed': False}, env)
    assert path.exists()
    assert await cleanup_keypair(OPTS, owned, {'all_resources_destroyed': True}, env) == {'mode': 'managed', 'cleaned': True}
    assert not path.exists()
    assert await cleanup_keypair(OPTS, owned, {'all_resources_destroyed': True}, env) == {'mode': 'managed', 'cleaned': True}


@pytest.mark.asyncio
async def test_failed_prepared_ack_preserves_key_and_prevents_adoption(tmp_path):
    env = environment(tmp_path)
    with pytest.raises(SSHError, match='ownership update failed'):
        await prepare_keypair(OPTS, {'status': 'fresh'}, env, lambda: True, lambda *_: False)
    assert (tmp_path / '.ssh/demo').exists()
    with pytest.raises(SSHError, match='unowned SSH key files'):
        await prepare_keypair(OPTS, {'status': 'fresh'}, env, lambda: True, lambda *_: True)


@pytest.mark.asyncio
async def test_distinct_backend_contenders_cannot_generate_same_local_profile(tmp_path):
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def intent():
        entered.set()
        await proceed.wait()
        return True
    env = environment(tmp_path)
    first = asyncio.create_task(prepare_keypair(OPTS, {'status': 'fresh'}, env, intent, lambda *_: True))
    await entered.wait()
    try:
        with pytest.raises(SSHError, match='reserved'):
            await prepare_keypair(OPTS, {'status': 'fresh'}, env, lambda: pytest.fail('must not claim'), lambda *_: True)
    finally:
        proceed.set()
    result = await first
    assert result['fingerprint'].startswith('SHA256:')
    assert not (tmp_path / '.ssh/.demo.colors-key.lock').exists()


@pytest.mark.asyncio
async def test_stale_reservation_never_removed(tmp_path):
    directory = tmp_path / '.ssh'
    directory.mkdir()
    lock = directory / '.demo.colors-key.lock'
    lock.write_text('unresolved prior invocation')
    with pytest.raises(SSHError, match='reserved'):
        await prepare_keypair(OPTS, {'status': 'fresh'}, environment(tmp_path), lambda: True, lambda *_: True)
    assert lock.read_text() == 'unresolved prior invocation'


@pytest.mark.asyncio
async def test_build_and_optout_do_not_touch_files(tmp_path):
    env = environment(tmp_path)
    key = await prepare_keypair({**OPTS, 'blue/event': 'build'}, {'status': 'error'}, env, None, None)
    assert key['private_key_path'] == '$HOME/.ssh/demo'
    external = {**OPTS, 'vultr-ssh-keys': ['external-id'], 'ssh-private-key-path': '~/external'}
    result = await prepare_keypair(external, {'status': 'error'}, env, None, None)
    assert result['reference'] == ['external-id']
    assert await cleanup_keypair(external, None, None, env) == result
    assert not (tmp_path / '.ssh').exists()


@pytest.mark.asyncio
async def test_missing_owned_key_and_symlink_refused(tmp_path):
    owned = {'status': 'prepared', 'fingerprint': 'SHA256:' + 'a' * 43}
    with pytest.raises(SSHError, match='missing'):
        await prepare_keypair(OPTS, owned, environment(tmp_path), None, None)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (tmp_path / '.ssh').symlink_to(elsewhere)
    with pytest.raises(SSHError, match='unsafe SSH'):
        await prepare_keypair(OPTS, {'status': 'fresh'}, environment(tmp_path), None, None)
    assert not list(elsewhere.iterdir())


@pytest.mark.asyncio
async def test_partial_cleanup_verifies_remaining_material(tmp_path):
    env = environment(tmp_path)
    result = await prepare_keypair(OPTS, {'status': 'fresh'}, env, lambda: True, lambda *_: True)
    Path(result['private_key_path']).unlink()
    await cleanup_keypair(OPTS, {'status': 'prepared', 'fingerprint': result['fingerprint']}, {'all_resources_destroyed': True}, env)
    assert not Path(result['public_key_path']).exists()
