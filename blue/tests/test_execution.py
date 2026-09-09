import asyncio
from copy import deepcopy
import json
from pathlib import Path
import stat

import pytest

from colors_compute.backend import ProcessResult
from colors_compute.execution import converge_state, state_presence

OPTS = {'profile': 'demo', 'provider-compute': 'vultr', 'provider-backend': 'r2', 'r2-bucket': 'states', 'r2-endpoint': 'https://example.invalid'}
ENV = {'AWS_PROFILE': 'compute-sso', 'AWS_SESSION_TOKEN': 'ambient-token',
       'AWS_ACCESS_KEY_ID': 'ambient-access', 'AWS_SECRET_ACCESS_KEY': 'ambient-secret',
       'COLORS_PAR_R2_ACCESS_KEY_ID': 'fixture-r2-access', 'COLORS_PAR_R2_SECRET_ACCESS_KEY': 'fixture-r2-secret',
       'COLORS_PAR_VULTR_API_KEY': 'fixture-vultr-token', 'TF_CLI_ARGS': '-lock=false', 'TF_LOG': 'TRACE'}
KEY = 'demo/compute/nodes/0.tfstate'
DOCUMENTS = {'node.tf.json': {'resource': {'vultr_instance': {'node': {'label': 'demo-0'}}}}}
STATE = {'version': 4, 'serial': 1, 'lineage': 'fixture', 'resources': [{'type': 'vultr_instance'}],
         'outputs': {'params': {'value': {'provider': 'vultr', 'ip': '192.0.2.1'}}}}
EMPTY = {**STATE, 'resources': [], 'outputs': {}}
PLAN = {'format_version': '1.2', 'planned_values': {}, 'resource_changes': [{'change': {'actions': ['create']}}]}


class Runner:
    def __init__(self, before='', after=None, plan=None, failure=None):
        self.before, self.after, self.plan = before, json.dumps(STATE) if after is None else after, PLAN if plan is None else plan
        self.failure = failure
        self.calls, self.paths = [], []
    async def __call__(self, command, cwd, env, timeout):
        self.calls.append(command)
        self.paths.append(Path(cwd))
        assert stat.S_IMODE(Path(cwd).stat().st_mode) == 0o700
        for path in Path(cwd).iterdir():
            if path.is_file(): assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert env['AWS_PROFILE'] == ENV['AWS_PROFILE']
        assert env['AWS_SESSION_TOKEN'] == ENV['AWS_SESSION_TOKEN']
        assert env['VULTR_API_KEY'] == ENV['COLORS_PAR_VULTR_API_KEY']
        assert 'TF_LOG' not in env and 'TF_CLI_ARGS' not in env
        assert not any(k.startswith('COLORS_PAR_') for k in env)
        assert env['AWS_ACCESS_KEY_ID'] == ENV['AWS_ACCESS_KEY_ID']
        credentials = json.loads((Path(cwd) / 'credentials.tfbackend.json').read_text())
        assert credentials == {'access_key': ENV['COLORS_PAR_R2_ACCESS_KEY_ID'], 'secret_key': ENV['COLORS_PAR_R2_SECRET_ACCESS_KEY']}
        assert timeout == (1800000 if command[1] in ('plan', 'apply') else 120000)
        if self.failure == command[1]: return ProcessResult(-1, '', 'secret failure')
        if command[1:3] == ['state', 'pull']:
            return ProcessResult(0, self.after if any(c[1] == 'apply' for c in self.calls) else self.before)
        if command[1] == 'show': return ProcessResult(0, json.dumps(self.plan))
        return ProcessResult(0, '')


@pytest.mark.asyncio
async def test_new_node_fixed_sequence_and_private_combined_credentials():
    runner = Runner()
    result = await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'absent'}, ENV, runner)
    assert result == {'status': 'ready', 'params': STATE['outputs']['params']['value'], 'outputs': {'params': STATE['outputs']['params']['value']}}
    assert [c[1] for c in runner.calls] == ['init', 'state', 'plan', 'show', 'apply', 'state']
    assert runner.calls[4][-1] == runner.calls[3][-1]
    assert not runner.paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('before', ['', 'bad JSON', json.dumps({**STATE, 'outputs': {}}), json.dumps({**STATE, 'outputs': {'params': {'value': {'provider': 'aws'}}}})])
async def test_present_legacy_mismatched_or_unreadable_state_refused(before):
    runner = Runner(before=before)
    assert await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'present'}, ENV, runner) == {'status': 'error'}
    assert all(c[1] != 'plan' for c in runner.calls)
    assert not runner.paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('actions', [['delete', 'create'], ['delete'], ['create', 'delete'], ['unknown'], [], None])
async def test_replacement_and_unknown_actions_never_apply(actions):
    runner = Runner(plan={**PLAN, 'resource_changes': [{'change': {'actions': actions}}]})
    assert await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'absent'}, ENV, runner) == {'status': 'error'}
    assert all(c[1] != 'apply' for c in runner.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['init', 'state', 'plan', 'show', 'apply'])
async def test_failed_commands_fail_closed(failure):
    runner = Runner(failure=failure)
    assert await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'absent'}, ENV, runner) == {'status': 'error'}
    assert not runner.paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('after', ['', '{}', json.dumps(EMPTY), json.dumps({**STATE, 'outputs': {'params': {'value': {'provider': 'vultr', 'secret': 'fixture-vultr-token'}}}})])
async def test_post_apply_missing_or_secret_outputs_fail(after):
    assert await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'absent'}, ENV, Runner(after=after)) == {'status': 'error'}


@pytest.mark.asyncio
async def test_delete_guards_and_confirmed_absence():
    async def never(*args): pytest.fail('no command allowed')
    assert await converge_state(OPTS, KEY, DOCUMENTS, 'delete', {'status': 'absent'}, ENV, never) == {'status': 'error'}
    opts = {**OPTS, 'compute-prevent-destroy': False}
    assert await converge_state(opts, KEY, DOCUMENTS, 'delete', {'status': 'absent'}, ENV, never) == {'status': 'destroyed'}
    runner = Runner(before=json.dumps(STATE), after=json.dumps(EMPTY), plan={**PLAN, 'resource_changes': [{'change': {'actions': ['delete']}}]})
    assert await converge_state(opts, KEY, DOCUMENTS, 'delete', {'status': 'present'}, ENV, runner) == {'status': 'destroyed'}
    assert '-destroy' in runner.calls[2]


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['other/compute/shared.tfstate', 'demo/other', 'demo/compute/nodes/../x.tfstate'])
async def test_arbitrary_state_keys_refuse(key):
    async def never(*args): pytest.fail('no command allowed')
    assert await state_presence(OPTS, key, ENV, never) == {'status': 'error'}
    assert await converge_state(OPTS, key, DOCUMENTS, 'create', {'status': 'absent'}, ENV, never) == {'status': 'error'}


@pytest.mark.asyncio
@pytest.mark.parametrize('document', [
    {'../node.tf.json': {}}, {'backend.tf.json': {}},
    {'node.tf.json': {'terraform': {'backend': {}}}},
    {'node.tf.json': {'provider': {'aws': {}}}},
    {'node.tf.json': {'terraform': {'required_providers': {'vultr': {'source': 'evil/provider'}}}}},
    {'node.tf.json': {'resource': {'vultr_instance': {'node': {'provisioner': {}}}}}},
])
async def test_provider_and_document_overrides_refuse(document):
    async def never(*args): pytest.fail('no command allowed')
    assert await converge_state(OPTS, KEY, document, 'create', {'status': 'absent'}, ENV, never) == {'status': 'error'}


@pytest.mark.asyncio
@pytest.mark.parametrize('code,expected', [('NoSuchKey', 'absent'), ('NoSuchBucket', 'error'), ('AccessDenied', 'error')])
async def test_state_presence_exact_missing_key_only(code, expected):
    paths = []
    async def runner(command, cwd, child, timeout):
        paths.append(Path(cwd))
        assert command[2] == 'get-object' and command[6] == KEY
        assert 'AWS_PROFILE' not in child
        return ProcessResult(1, '', f'aws: [ERROR]: An error occurred ({code}) when calling the GetObject operation (reached max retries: 0): fixture')
    assert await state_presence(OPTS, KEY, ENV, runner) == {'status': expected}
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_cancellation_propagates_after_cleanup():
    paths = []
    async def runner(command, cwd, env, timeout):
        paths.append(Path(cwd)); raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'absent'}, ENV, runner)
    assert not paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', [{'value': 'sensitive', 'sensitive': True}, {'value': 'bad', 'sensitive': None}, {'type': 'string'}, 'invalid', {'value': ENV['COLORS_PAR_VULTR_API_KEY']}])
async def test_shared_outputs_reject_sensitive_malformed_or_secret_values(entry):
    state = deepcopy(STATE)
    state['outputs']['ssh_key_id'] = entry
    assert await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'absent'}, ENV, Runner(after=json.dumps(state))) == {'status': 'error'}


@pytest.mark.asyncio
async def test_shared_outputs_flatten_key_references():
    state = deepcopy(STATE)
    state['outputs']['ssh_key_id'] = {'value': 'key-reference', 'sensitive': False, 'type': 'string'}
    result = await converge_state(OPTS, KEY, DOCUMENTS, 'create', {'status': 'absent'}, ENV, Runner(after=json.dumps(state)))
    assert result['outputs']['ssh_key_id'] == 'key-reference'
