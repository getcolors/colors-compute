import hashlib
import json
from pathlib import Path
import stat

import pytest

from colors_compute.backend import ProcessResult, read_state
from colors_compute.contract import validate, credential_requirements, local_state_dir
from colors_compute.coordination import coordination
from colors_compute.execution import converge_state, state_presence
from colors_compute.journal import journal_get, journal_put
from colors_compute.managed_journal import managed_coordination
from colors_compute.rendering import backend_plan

KEY = 'demo/compute/nodes/0.tfstate'


def options(path):
    return {'profile': 'demo', 'provider-compute': 'aws', 'provider-backend': 'local', 'local-state-dir': str(path)}


def identity(opts):
    return {'profile': 'demo', 'provider': 'aws', 'backend': {'kind': 'local', 'path': local_state_dir(opts)}}


def acquire(opts, managed=False):
    reducer = managed_coordination if managed else coordination
    return reducer({'status': 'absent'}, identity(opts), {'type': 'managed/acquire' if managed else 'acquire',
                   'run_id': 'run-1', 'write_id': 'write-1', 'target_etag': None})


def test_local_plan_persistent_path_and_no_credentials(tmp_path):
    opts = options(tmp_path / 'state')
    assert backend_plan(opts, KEY) == {'config': {'terraform': {'backend': {'local': {'path': str(tmp_path / 'state' / KEY)}}}},
                                      'credential_bindings': {}, 'environment': {}}
    assert credential_requirements(opts) == []
    assert not (tmp_path / 'state').exists()
    assert backend_plan(options('/'), KEY)['config']['terraform']['backend']['local']['path'] == '/' + KEY


def test_default_local_plan_is_stable_and_does_not_create_directories(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    opts = {'profile': 'demo', 'provider-compute': 'aws', 'provider-backend': 'local'}
    expected = str(home / '.local/state/colors' / KEY)
    assert not any('local-state-dir' in error for error in validate(opts))
    first = backend_plan(opts, KEY)
    monkeypatch.chdir(tmp_path)
    assert backend_plan(opts, KEY) == first
    assert first['config']['terraform']['backend']['local']['path'] == expected
    assert not home.exists()
    assert 'local-state-dir' not in opts
    assert local_state_dir(options('/explicit')) == '/explicit'
    monkeypatch.setenv('HOME', '/')
    assert local_state_dir(opts) == '/.local/state/colors'


@pytest.mark.parametrize('home', [None, '', 'relative', '/tmp/../home', '/tmp/home/'])
def test_default_local_path_requires_valid_home(home, monkeypatch):
    if home is None:
        monkeypatch.delenv('HOME', raising=False)
    else:
        monkeypatch.setenv('HOME', home)
    opts = {'profile': 'demo', 'provider-compute': 'aws', 'provider-backend': 'local'}
    assert [error for error in validate(opts) if 'local-state-dir' in error] == [':local-state-dir must be an absolute normalized POSIX path']
    with pytest.raises(ValueError, match='absolute normalized POSIX path'):
        backend_plan(opts, KEY)
    assert not any('local-state-dir' in error for error in validate(options('/explicit')))
    assert local_state_dir(options('/explicit')) == '/explicit'


@pytest.mark.parametrize('path', [None, '', ' ', 'REPLACE_ME'])
def test_explicit_missing_local_path_does_not_default(path):
    opts = {**options('/unused'), 'local-state-dir': path}
    assert [error for error in validate(opts) if 'local-state-dir' in error] == [':local-state-dir is required']
    with pytest.raises(ValueError, match=':local-state-dir is required'):
        backend_plan(opts, KEY)


@pytest.mark.parametrize('path', ['relative', '/tmp/../state', '/tmp/./state', '/tmp//state', '/tmp/state/', '//tmp/state', '/tmp\\state', '/tmp/\0state', 3])
def test_invalid_local_path(path):
    opts = {**options('/unused'), 'local-state-dir': path}
    message = ':local-state-dir must be an absolute normalized POSIX path'
    assert message in validate(opts)
    with pytest.raises(ValueError, match='absolute normalized POSIX path'):
        backend_plan(opts, KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize('managed', [False, True])
async def test_journal_cas_private_atomic_and_interoperable_digest(tmp_path, managed):
    opts = options(tmp_path / 'state')
    target = Path(opts['local-state-dir']) / 'demo/compute/coordination.json'
    assert await journal_get(opts) == {'status': 'absent'}
    assert not target.parent.exists()
    intent = acquire(opts, managed)
    written = await journal_put(opts, intent)
    assert written == {'status': 'written', 'etag': hashlib.sha256(target.read_bytes()).hexdigest()}
    assert await journal_get(opts) == {'status': 'present', 'etag': written['etag'], 'document': intent['document']}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in [target.parent, target.parent.parent, target.parent.parent.parent])
    assert await journal_put(opts, intent) == {'status': 'conflict'}
    assert await journal_put(opts, {**intent, 'condition': {'if_match': 'stale'}}) == {'status': 'conflict'}
    assert (await journal_put(opts, {**intent, 'condition': {'if_match': written['etag']}}))['status'] == 'written'
    lock = Path(str(target) + '.lock')
    lock.mkdir()
    assert await journal_put(opts, intent) == {'status': 'conflict'}
    assert lock.is_dir()


@pytest.mark.asyncio
async def test_default_journal_uses_process_home_and_resolved_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    opts = {'profile': 'demo', 'provider-compute': 'aws', 'provider-backend': 'local'}
    target = tmp_path / '.local/state/colors/demo/compute/coordination.json'
    intent = acquire(opts)
    environment = {'HOME': str(tmp_path / 'other-home')}
    assert await journal_get(opts, environment) == {'status': 'absent'}
    assert not target.parent.exists()
    assert (await journal_put(opts, intent, environment))['status'] == 'written'
    assert json.loads(target.read_text())['identity']['backend']['path'] == str(tmp_path / '.local/state/colors')
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (tmp_path / 'other-home').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('content', [b'[]', b'{"a":NaN}', b'\xff', b' ' * (2 * 1024 * 1024 + 1)])
async def test_bad_journal_cannot_be_overwritten(tmp_path, content):
    opts = options(tmp_path)
    target = tmp_path / 'demo/compute/coordination.json'
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    assert await journal_get(opts) == {'status': 'error'}
    assert await journal_put(opts, acquire(opts)) == {'status': 'error'}
    assert target.read_bytes() == content
    assert not Path(str(target) + '.lock').exists()


@pytest.mark.asyncio
async def test_presence_requires_regular_readable_file_and_rejects_symlinks(tmp_path):
    opts = options(tmp_path)
    target = tmp_path / KEY
    assert await state_presence(opts, KEY) == {'status': 'absent'}
    target.parent.mkdir(parents=True)
    target.write_text('{}')
    assert await state_presence(opts, KEY) == {'status': 'present'}
    target.unlink()
    target.symlink_to(tmp_path / 'missing')
    assert await state_presence(opts, KEY) == {'status': 'error'}
    target.unlink()
    target.mkdir()
    assert await state_presence(opts, KEY) == {'status': 'error'}
    linked = tmp_path / 'linked'
    linked.symlink_to(tmp_path / 'missing', target_is_directory=True)
    assert await state_presence(options(linked), KEY) == {'status': 'error'}


@pytest.mark.asyncio
@pytest.mark.parametrize('use_default', [False, True])
async def test_converge_keeps_local_state_after_workdir_cleanup(tmp_path, monkeypatch, use_default):
    monkeypatch.setenv('HOME', str(tmp_path))
    opts = {**options(tmp_path / 'state'), 'provider-compute': 'vultr'}
    if use_default:
        del opts['local-state-dir']
    target = Path(local_state_dir(opts)) / KEY
    documents = {'node.tf.json': {'resource': {'vultr_instance': {'node': {'label': 'demo-0'}}}}}
    state = {'version': 4, 'serial': 1, 'lineage': 'fixture', 'resources': [{'type': 'vultr_instance'}],
             'outputs': {'params': {'value': {'provider': 'vultr', 'ip': '192.0.2.1'}}}}
    calls, directories = [], []
    async def runner(command, cwd, env, timeout):
        calls.append(command[1])
        directories.append(Path(cwd))
        assert json.loads((Path(cwd) / 'backend.tf.json').read_text())['terraform']['backend']['local']['path'] == str(target)
        if command[1] == 'show':
            return ProcessResult(0, json.dumps({'format_version': '1.2', 'planned_values': {}, 'resource_changes': [{'change': {'actions': ['create']}}]}))
        if command[1] == 'apply':
            target.write_text(json.dumps(state))
            target.chmod(0o644)
        if command[1] == 'state':
            return ProcessResult(0, target.read_text())
        return ProcessResult(0, '')
    assert (await converge_state(opts, KEY, documents, 'create', {'status': 'absent'}, {'COLORS_PAR_VULTR_API_KEY': 'token'}, runner))['status'] == 'ready'
    assert calls == ['init', 'plan', 'show', 'apply', 'state']
    assert target.exists() and all(not path.exists() for path in directories)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert (await read_state(opts, KEY, {}, runner))['status'] == 'present'


@pytest.mark.asyncio
async def test_failed_apply_preserves_private_state_and_removes_workdir(tmp_path):
    opts = {**options(tmp_path / 'state'), 'provider-compute': 'vultr'}
    target = Path(opts['local-state-dir']) / KEY
    documents = {'node.tf.json': {'resource': {'vultr_instance': {'node': {'label': 'demo-0'}}}}}
    directories = []

    async def runner(command, cwd, env, timeout):
        directories.append(Path(cwd))
        if command[1] == 'show':
            return ProcessResult(0, json.dumps({'format_version': '1.2', 'planned_values': {}, 'resource_changes': [{'change': {'actions': ['create']}}]}))
        if command[1] == 'apply':
            target.write_text('{}')
            target.chmod(0o644)
            Path(str(target) + '.backup').write_text('{}')
            return ProcessResult(1, '', 'failed apply')
        return ProcessResult(0, '')

    assert await converge_state(opts, KEY, documents, 'create', {'status': 'absent'}, {'COLORS_PAR_VULTR_API_KEY': 'token'}, runner) == {'status': 'error'}
    assert all(not path.exists() for path in directories)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(str(target) + '.backup').stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_symlink_journal_directory_does_not_modify_target(tmp_path):
    target = tmp_path / 'target'
    target.mkdir(mode=0o755)
    linked = tmp_path / 'linked'
    linked.symlink_to(target, target_is_directory=True)
    opts = options(linked)
    assert await journal_put(opts, acquire(opts)) == {'status': 'error'}
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == []
