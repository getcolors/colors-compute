import base64
import hashlib
import json
from pathlib import Path
import stat

import pytest

from colors_compute.backend import ProcessResult
from colors_compute.node import node_plan, build_node, compute_node

CASES = json.loads((Path(__file__).parents[2] / 'test/fixtures/provider-requests.json').read_text())
PUBLIC = 'ssh-ed25519 ' + base64.b64encode(b'test-public-key').decode()
FINGERPRINT = 'SHA256:' + base64.b64encode(hashlib.sha256(b'test-public-key').digest()).decode().rstrip('=')


def inputs(tmp_path, provider='digitalocean'):
    args = next(c['args'] for c in CASES if c['args'][0]['provider-compute'] == provider and c['args'][1] == 'shared')
    return ({**args[0], 'provider-backend': 's3', 's3-bucket': 'state-bucket', 's3-region': 'eu-west-1', 's3-prefix': 'infra'},
            {'node_id': 'app-0', 'state_filename': 'app-0.tfstate', 'workdir': str(tmp_path),
             'security': args[2]['security'], 'network': args[2]['network']})


@pytest.mark.parametrize('provider', ['aws', 'azure', 'digitalocean', 'google', 'hcloud', 'oci', 'vultr', 'yandex'])
def test_all_provider_roots(tmp_path, provider):
    opts, req = inputs(tmp_path, provider)
    result = node_plan(opts, req)
    root = result['documents']['compute.tf.json']
    assert result['state_key'] == 'infra/' + opts['profile'] + '/app-0.tfstate'
    assert result['directory'] == str(tmp_path / opts['profile'] / 'app-0')
    assert root['resource']['tls_private_key']['machine']['algorithm'] == 'ED25519'
    assert root['resource']['aws_s3_object']['ssh_private']['provider'] == 'aws.keys'
    assert root['provider']['aws'][-1]['alias'] == 'keys'
    assert root['output']['compute_identity']['value']['profile'] == opts['profile']
    assert 'colors-shared-reference' not in json.dumps(root)
    assert 'colors-public-key-placeholder' not in json.dumps(root)
    assert 'BEGIN OPENSSH' not in json.dumps(root)
    for kind, instances in root['resource'].items():
        if kind not in ('tls_private_key', 'aws_s3_object'):
            for resource in instances.values():
                assert 'aws_s3_object.ssh_private' in resource['depends_on']


def test_build_persistent_and_isolated(tmp_path):
    opts, req = inputs(tmp_path)
    one = build_node(opts, req)
    root = Path(one['directory'])
    (root / '.terraform').mkdir()
    (root / '.terraform' / 'retained').write_text('keep')
    (root / 'ssh-key').write_text('local never read')
    build_node(opts, req)
    two = build_node(opts, {**req, 'node_id': 'app-1', 'state_filename': 'app-1.tfstate'})
    assert one['state_key'] != two['state_key']
    assert (root / '.terraform' / 'retained').read_text() == 'keep'
    assert (root / 'ssh-key').read_text() == 'local never read'
    assert stat.S_IMODE((root / 'compute.tf.json').stat().st_mode) == 0o600


@pytest.mark.parametrize('field,value', [('node_id', '../escape'), ('state_filename', '../x.tfstate'), ('state_filename', 'x/y.tfstate'), ('workdir', 'relative')])
def test_invalid_identity(tmp_path, field, value):
    opts, req = inputs(tmp_path)
    with pytest.raises(ValueError):
        node_plan(opts, {**req, field: value})


def test_build_refuses_provider_or_backend_rebind(tmp_path):
    opts, req = inputs(tmp_path)
    first = build_node(opts, req)
    original = (Path(first['directory']) / 'compute.tf.json').read_text()
    other_opts, other_req = inputs(tmp_path, 'aws')
    other_opts['profile'] = opts['profile']
    with pytest.raises(ValueError):
        build_node(other_opts, other_req)
    with pytest.raises(ValueError):
        build_node({**opts, 's3-prefix': 'other'}, req)
    assert (Path(first['directory']) / 'compute.tf.json').read_text() == original


class Runner:
    def __init__(self, opts, req, *, existing=False, fail_get=False, actions=None, wrong_provider=False):
        self.opts, self.req = opts, req
        self.existing, self.fail_get = existing, fail_get
        self.actions = actions
        self.wrong_provider = wrong_provider
        self.calls = []
        self.destroyed = False

    def outputs(self):
        return {'params': {'value': {'provider': 'aws' if self.wrong_provider else self.opts['provider-compute'], 'node_id': self.req['node_id'], 'ip': '192.0.2.1', 'user': 'root'}},
                'compute_identity': {'value': {'profile': self.opts['profile'], 'node_id': self.req['node_id'], 'state_filename': self.req['state_filename'], 'provider': self.opts['provider-compute']}},
                'ssh_public_key_fingerprint': {'value': FINGERPRINT}}

    async def __call__(self, args, cwd, env, timeout):
        self.calls.append(args)
        assert cwd == str(Path(self.req['workdir']) / self.opts['profile'] / self.req['node_id'])
        assert 'TF_DATA_DIR' not in env
        if args[:3] == ['tofu', 'show', '-json']:
            if len(args) == 4:
                return ProcessResult(0, json.dumps({'format_version': '1.2', 'planned_values': {}, 'resource_changes': [{'change': {'actions': self.actions or ['create']}}]}))
            return ProcessResult(0, json.dumps({'values': {'root_module': {'resources': [{'type': 'digitalocean_droplet'}] if self.existing and not self.destroyed else []}, 'outputs': self.outputs()}}))
        if args[:3] == ['tofu', 'state', 'pull']:
            return ProcessResult(0, json.dumps({'version': 4, 'serial': 1, 'lineage': 'test', 'resources': [], 'outputs': {} if self.destroyed else self.outputs()}))
        if args[:2] == ['tofu', 'apply']:
            self.existing = True
            self.destroyed = self.actions == ['delete']
        if args[:3] == ['aws', 's3api', 'head-object']:
            return ProcessResult(1, '', 'An error occurred (404) when calling the HeadObject operation: Not Found')
        if args[:3] == ['aws', 's3api', 'get-object']:
            key = args[args.index('--key') + 1]
            dest = args[args.index('--key') + 2]
            if not self.existing:
                return ProcessResult(1, '', 'An error occurred (NoSuchKey) when calling the GetObject operation: missing')
            if key.endswith('.tfstate'):
                Path(dest).write_text(json.dumps({'version': 4, 'serial': 1, 'lineage': 'test', 'resources': [{'type': 'digitalocean_droplet'}, *[{'mode': 'managed', 'type': 'aws_s3_object', 'name': 'ssh_' + kind, 'instances': [{'attributes': {'bucket': 'state-bucket', 'key': node_plan(self.opts, self.req)['key_objects'][kind]}}]} for kind in ('private', 'public')]], 'outputs': self.outputs()}))
                return ProcessResult(0, '{}')
            if self.fail_get:
                return ProcessResult(1, '', 'denied')
            Path(dest).write_text(PUBLIC if args[args.index('--key') + 1].endswith('.pub') else 'remote-private')
        if args[0] == 'ssh-keygen':
            return ProcessResult(0, PUBLIC)
        return ProcessResult(0, '{}')


async def execute(opts, req, runner, operation='create'):
    return await compute_node(opts, req, operation, {'PATH': '/usr/bin', 'COLORS_PAR_DO_TOKEN': 'secret', 'TF_DATA_DIR': '/wrong'}, {'runner': runner})


@pytest.mark.asyncio
async def test_create_refreshes_remote_keys(tmp_path):
    opts, req = inputs(tmp_path)
    result = build_node(opts, req)
    root = Path(result['directory'])
    (root / 'ssh-key').write_text('stale')
    runner = Runner(opts, req)
    result = await execute(opts, req, runner)
    assert result['status'] == 'ready'
    assert (root / 'ssh-key').read_text() == 'remote-private'
    assert (root / 'ssh-key.pub').read_text() == PUBLIC
    assert not (root / 'credentials.tfbackend.json').exists()
    assert 'remote-private' not in json.dumps(result)


@pytest.mark.asyncio
async def test_remote_failure_removes_stale_access(tmp_path):
    opts, req = inputs(tmp_path)
    root = Path(build_node(opts, req)['directory'])
    (root / 'ssh-key').write_text('stale')
    runner = Runner(opts, req, existing=True, fail_get=True)
    assert (await execute(opts, req, runner, 'prepare-access'))['status'] == 'error'
    assert not (root / 'ssh-key').exists()
    assert not list(root.glob('*.download'))


@pytest.mark.asyncio
async def test_provider_mismatch_and_replacement_refused(tmp_path):
    opts, req = inputs(tmp_path)
    for runner in (Runner(opts, req, existing=True, wrong_provider=True), Runner(opts, req, actions=['delete', 'create'])):
        assert (await execute(opts, req, runner))['status'] == 'error'
        assert not any(args[:2] == ['tofu', 'apply'] for args in runner.calls)


@pytest.mark.asyncio
async def test_delete_retains_templates(tmp_path):
    opts, req = inputs(tmp_path)
    opts['compute-prevent-destroy'] = False
    root = Path(build_node(opts, req)['directory'])
    (root / 'ssh-key').write_text('stale')
    runner = Runner(opts, req, existing=True, actions=['delete'])
    assert (await execute(opts, req, runner, 'delete'))['status'] == 'destroyed'
    assert (root / 'compute.tf.json').exists()
    assert not (root / 'ssh-key').exists()


@pytest.mark.asyncio
async def test_unreadable_state_and_foreign_keys_never_apply(tmp_path):
    opts, req = inputs(tmp_path)
    class Denied(Runner):
        async def __call__(self, args, cwd, env, timeout):
            if args[:3] == ['aws', 's3api', 'get-object']:
                self.calls.append(args)
                return ProcessResult(1, '', 'An error occurred (404) when calling the GetObject operation: ambiguous')
            return await super().__call__(args, cwd, env, timeout)
    runner = Denied(opts, req)
    assert (await execute(opts, req, runner))['status'] == 'error'
    assert not any(args[:2] == ['tofu', 'apply'] for args in runner.calls)


@pytest.mark.asyncio
async def test_required_state_refuses_first_create(tmp_path):
    opts, req = inputs(tmp_path)
    opts['compute-require-existing-state'] = True
    runner = Runner(opts, req)
    assert (await execute(opts, req, runner))['status'] == 'error'
    assert not any(args[:2] == ['tofu', 'apply'] for args in runner.calls)


@pytest.mark.asyncio
async def test_secret_outputs_do_not_escape(tmp_path):
    opts, req = inputs(tmp_path)
    class SecretOutput(Runner):
        def outputs(self):
            value = super().outputs()
            value['params']['value']['metadata'] = {'private_key': 'opaque-secret'}
            return value
    assert (await execute(opts, req, SecretOutput(opts, req, existing=True), 'inspect'))['status'] == 'error'


def test_symlink_substitution_and_normalized_paths(tmp_path):
    opts, req = inputs(tmp_path)
    plan = build_node(opts, req)
    root = Path(plan['directory'])
    template = root / 'compute.tf.json'
    outside = tmp_path / 'outside'
    outside.write_text('retained')
    template.unlink()
    template.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        build_node(opts, req)
    assert outside.read_text() == 'retained'
    for workdir in (str(tmp_path) + '/../escape', str(tmp_path) + '/', str(tmp_path) + '//x'):
        with pytest.raises(ValueError):
            node_plan(opts, {**req, 'workdir': workdir})


def test_sdk_frozen_inputs_and_local_state(tmp_path):
    from types import MappingProxyType
    opts, req = inputs(tmp_path)
    frozen = node_plan(MappingProxyType(opts), MappingProxyType(req))
    assert frozen == node_plan(opts, req)
    local = node_plan({**opts, 'provider-backend': 'local'}, req)
    assert local['documents']['backend.tf.json']['terraform']['backend']['local']['path'] == str(Path(local['directory']) / req['state_filename'])


@pytest.mark.asyncio
async def test_partial_state_cannot_overwrite_unowned_remote_key(tmp_path):
    opts, req = inputs(tmp_path)
    class PartialState(Runner):
        async def __call__(self, args, cwd, env, timeout):
            result = await super().__call__(args, cwd, env, timeout)
            if args[:3] == ['aws', 's3api', 'get-object'] and args[args.index('--key') + 1].endswith('.tfstate'):
                target = Path(args[args.index('--key') + 2])
                state = json.loads(target.read_text())
                state['resources'] = [resource for resource in state['resources'] if resource.get('name') != 'ssh_private']
                target.write_text(json.dumps(state))
            return result
    runner = PartialState(opts, req, existing=True)
    assert (await execute(opts, req, runner))['status'] == 'error'
    assert not any(args[:2] == ['tofu', 'apply'] for args in runner.calls)


@pytest.mark.asyncio
async def test_missing_state_cannot_authorize_delete(tmp_path):
    opts, req = inputs(tmp_path)
    opts['compute-prevent-destroy'] = False
    runner = Runner(opts, req)
    assert (await execute(opts, req, runner, 'delete'))['status'] == 'error'
    assert not any(args[:2] == ['tofu', 'apply'] for args in runner.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,extra', [('azure', {'nic_id': 'nic-1', 'subnet_id': 'subnet-1', 'vm_id': 'vm-1'}), ('hcloud', {'server_id': '1', 'network_attachment_id': 'attachment'})])
async def test_provider_specific_outputs_are_supported(tmp_path, provider, extra):
    opts, req = inputs(tmp_path, provider)
    class ProviderOutput(Runner):
        def outputs(self):
            outputs = super().outputs()
            outputs['params']['value'].update(extra)
            return outputs
    # Azure uses ambient auth; hcloud requires its provider token.
    env = {'PATH': '/usr/bin', 'COLORS_PAR_HCLOUD_TOKEN': 'test-token'}
    result = await compute_node(opts, req, 'inspect', env, {'runner': ProviderOutput(opts, req, existing=True)})
    assert result['status'] == 'ready'
    assert extra.items() <= result['params'].items()


@pytest.mark.parametrize('provider', ['aws', 'azure', 'digitalocean', 'google', 'hcloud', 'oci', 'vultr', 'yandex'])
def test_local_plan_has_no_remote_key_dependency(tmp_path, provider):
    opts, req = inputs(tmp_path, provider)
    plan = node_plan({**opts, 'provider-backend': 'local'}, req)
    root = plan['documents']['compute.tf.json']
    assert plan['key_objects'] == {}
    assert 'aws_s3_object' not in root['resource']
    assert 'variable' not in root
    assert root['output']['ssh_private_key'] == {'value': '${tls_private_key.machine.private_key_openssh}', 'sensitive': True}
    assert root['output']['ssh_public_key'] == {'value': '${tls_private_key.machine.public_key_openssh}'}
    if provider != 'aws':
        assert 'aws' not in root['provider']
        assert 'aws' not in root['terraform']['required_providers']
    else:
        assert isinstance(root['provider']['aws'], dict)
        assert 'alias' not in root['provider']['aws']
    for kind, instances in root['resource'].items():
        if kind != 'tls_private_key':
            assert all('tls_private_key.machine' in resource['depends_on'] for resource in instances.values())


@pytest.mark.parametrize('option', ['ssh-s3-bucket', 'ssh-s3-region', 'ssh-s3-endpoint'])
def test_local_rejects_remote_key_settings(tmp_path, option):
    opts, req = inputs(tmp_path)
    with pytest.raises(ValueError):
        node_plan({**opts, 'provider-backend': 'local', option: 'unexpected'}, req)


class LocalRunner(Runner):
    def outputs(self):
        return {**super().outputs(), 'ssh_private_key': {'value': 'local-state-private', 'sensitive': True},
                'ssh_public_key': {'value': PUBLIC}}

    def save_state(self):
        root = Path(node_plan(self.opts, self.req)['directory'])
        root.mkdir(parents=True, exist_ok=True)
        (root / self.req['state_filename']).write_text(json.dumps({'version': 4, 'serial': 1, 'lineage': 'local-test',
            'resources': [] if self.destroyed else [{'type': 'digitalocean_droplet'}],
            'outputs': {} if self.destroyed else self.outputs()}))

    async def __call__(self, args, cwd, env, timeout):
        assert args[0] not in ('aws', 'gcloud')
        assert not any(key.startswith('TF_VAR_keys_') for key in env)
        result = await super().__call__(args, cwd, env, timeout)
        if args[:2] == ['tofu', 'apply']:
            self.save_state()
        return result


@pytest.mark.asyncio
async def test_local_create_refresh_and_delete_without_s3(tmp_path):
    opts, req = inputs(tmp_path)
    opts['provider-backend'] = 'local'
    runner = LocalRunner(opts, req)
    result = await execute(opts, req, runner)
    assert result['status'] == 'ready'
    assert 'local-state-private' not in json.dumps(result)
    root = Path(result['directory'])
    assert (root / 'ssh-key').read_text() == 'local-state-private'
    assert stat.S_IMODE((root / 'ssh-key').stat().st_mode) == 0o600
    (root / 'ssh-key').write_text('stale')
    assert (await execute(opts, req, runner, 'prepare-access'))['status'] == 'ready'
    assert (root / 'ssh-key').read_text() == 'local-state-private'
    opts['compute-prevent-destroy'] = False
    runner.actions = ['delete']
    assert (await execute(opts, req, runner, 'delete'))['status'] == 'destroyed'
    assert not (root / 'ssh-key').exists()
    assert (root / 'compute.tf.json').exists()
    assert (root / req['state_filename']).exists()


@pytest.mark.asyncio
async def test_local_lost_state_never_adopts_existing_copies(tmp_path):
    opts, req = inputs(tmp_path)
    opts['provider-backend'] = 'local'
    root = Path(build_node(opts, req)['directory'])
    (root / 'ssh-key').write_text('sole-recovery-copy')
    runner = LocalRunner(opts, req)
    assert (await execute(opts, req, runner))['status'] == 'error'
    assert not any(args[:2] == ['tofu', 'apply'] for args in runner.calls)
    assert (root / 'ssh-key').read_text() == 'sole-recovery-copy'


@pytest.mark.asyncio
async def test_local_bad_fingerprint_removes_stale_access(tmp_path):
    opts, req = inputs(tmp_path)
    opts['provider-backend'] = 'local'
    class BadFingerprint(LocalRunner):
        def outputs(self):
            outputs = super().outputs()
            outputs['ssh_public_key_fingerprint']['value'] = 'SHA256:wrong'
            return outputs
    runner = BadFingerprint(opts, req, existing=True)
    runner.save_state()
    root = Path(node_plan(opts, req)['directory'])
    (root / 'ssh-key').write_text('stale')
    assert (await execute(opts, req, runner, 'prepare-access'))['status'] == 'error'
    assert not (root / 'ssh-key').exists()
    assert not list(root.glob('*.download'))


def test_local_state_does_not_depend_on_home(tmp_path, monkeypatch):
    opts, req = inputs(tmp_path)
    monkeypatch.delenv('HOME', raising=False)
    plan = node_plan({**opts, 'provider-backend': 'local'}, req)
    assert plan['documents']['backend.tf.json']['terraform']['backend']['local']['path'] == str(Path(plan['directory']) / req['state_filename'])


@pytest.mark.asyncio
async def test_inspect_strictly_empty_state_is_read_only_destroyed(tmp_path):
    opts, req = inputs(tmp_path)
    opts['provider-backend'] = 'local'
    runner = LocalRunner(opts, req)
    runner.destroyed = True
    runner.save_state()
    result = await execute(opts, req, runner, 'inspect')
    assert result == {'status': 'destroyed', 'directory': node_plan(opts, req)['directory']}
    assert not any(args[:2] in (['tofu', 'plan'], ['tofu', 'apply']) for args in runner.calls)
    assert (await execute(opts, req, runner, 'prepare-access'))['status'] == 'error'
    path = Path(result['directory']) / req['state_filename']
    state = json.loads(path.read_text())
    state['outputs'] = {'leftover': {'value': 'not empty'}}
    path.write_text(json.dumps(state))
    assert (await execute(opts, req, runner, 'inspect'))['status'] == 'error'
    path.unlink()
    assert (await execute(opts, req, runner, 'inspect'))['status'] == 'error'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'delete', 'inspect', 'prepare-access'])
async def test_output_only_state_requires_recovery_before_any_operation(tmp_path, operation):
    opts, req = inputs(tmp_path)
    opts.update({'provider-backend': 'local', 'compute-prevent-destroy': False})
    runner = LocalRunner(opts, req)
    runner.destroyed = True
    runner.save_state()
    root = Path(node_plan(opts, req)['directory'])
    path = root / req['state_filename']
    state = json.loads(path.read_text())
    state['outputs'] = {'leftover': {'value': 'incomplete state'}}
    path.write_text(json.dumps(state))
    (root / 'ssh-key').write_text('retain-for-recovery')
    assert (await execute(opts, req, runner, operation))['status'] == 'error'
    assert not any(args[:2] in (['tofu', 'plan'], ['tofu', 'apply']) for args in runner.calls)
    assert (root / 'ssh-key').read_text() == 'retain-for-recovery'
