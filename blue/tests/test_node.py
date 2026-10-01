"""Public-only compute and independently owned key registration lifecycle."""
import json
from pathlib import Path
import stat

import pytest
from colors_compute.backend import ProcessResult
from colors_compute.node import node_plan, build_node, compute_node, registration_plan, compute_registration

FIXTURES = Path(__file__).parents[2] / 'test/fixtures'
CASES = json.loads((FIXTURES / 'provider-requests.json').read_text())
IDENTITY = json.loads((FIXTURES / 'public-ssh.json').read_text())
REGISTRATIONS = {'aws': 'aws_key_pair', 'digitalocean': 'digitalocean_ssh_key', 'hcloud': 'hcloud_ssh_key', 'vultr': 'vultr_ssh_key'}


def inputs(tmp_path, provider='digitalocean'):
    args = next(c['args'] for c in CASES if c['args'][0]['provider-compute'] == provider and c['args'][1] == 'shared')
    opts = {**args[0], 'provider-backend': 'local'}
    req = {'node_id': 'app-0', 'state_filename': 'app-0.tfstate', 'workdir': str(tmp_path), 'security': args[2]['security'], 'network': args[2]['network'], 'ssh_resource': dict(IDENTITY)}
    if provider in REGISTRATIONS:
        req['ssh_registration'] = {'status': 'ready', 'reference': 'registration/fixture', 'id': '12345', 'provider': provider, 'ssh_resource_reference': IDENTITY['reference'], 'fingerprint': IDENTITY['fingerprint']}
    return opts, req


@pytest.mark.parametrize('provider', ['aws', 'azure', 'digitalocean', 'google', 'hcloud', 'oci', 'vultr', 'yandex'])
@pytest.mark.parametrize('backend', ['local', 'r2'])
def test_all_provider_public_only_roots(tmp_path, provider, backend):
    opts, req = inputs(tmp_path, provider)
    opts.update({'provider-backend': backend, 'r2-bucket': 'fixture', 'r2-endpoint': 'https://example.r2.cloudflarestorage.com', 's3-prefix': 'infra'})
    plan = node_plan(opts, req)
    root = plan['documents']['compute.tf.json']
    text = json.dumps(root)
    assert plan['key_objects'] == {}
    assert root['output']['compute_identity']['value']['ssh_resource_reference'] == IDENTITY['reference']
    assert not any(token in text for token in ['tls_private_key', 'aws_s3_object', 'private_key', 'sentinel', 'placeholder', 'keys_access_key'])
    assert not any(kind in root['resource'] for kind in REGISTRATIONS.values())
    assert plan['state_key'] == 'infra/' + opts['profile'] + '/app-0.tfstate'


@pytest.mark.parametrize('provider', list(REGISTRATIONS))
def test_registration_owns_exactly_one_public_resource(tmp_path, provider):
    opts, req = inputs(tmp_path, provider)
    plan = registration_plan(opts, {'name': 'access', 'workdir': str(tmp_path), 'state_filename': 'registration.tfstate', 'ssh_resource': IDENTITY})
    root = plan['documents']['compute.tf.json']
    assert set(root['resource']) == {REGISTRATIONS[provider]}
    assert root['output']['compute_identity']['value']['kind'] == 'ssh-registration'
    assert 'data' not in root and 'locals' not in root
    assert 'private_key' not in json.dumps(root)
    assert IDENTITY['public_key'] in json.dumps(root)


@pytest.mark.parametrize('field,value', [('node_id', '../x'), ('workdir', 'relative'), ('state_filename', '../x.tfstate'), ('ssh_resource', {}), ('ssh_registration', {})])
def test_invalid_request(tmp_path, field, value):
    opts, req = inputs(tmp_path)
    with pytest.raises(ValueError):
        node_plan(opts, {**req, field: value})


def test_fingerprint_and_registration_binding(tmp_path):
    opts, req = inputs(tmp_path)
    with pytest.raises(ValueError):
        node_plan(opts, {**req, 'ssh_resource': {**IDENTITY, 'fingerprint': 'SHA256:wrong'}})
    with pytest.raises(ValueError):
        node_plan(opts, {**req, 'ssh_registration': {**req['ssh_registration'], 'ssh_resource_reference': 'different'}})


def test_build_private_persistent_and_immutable(tmp_path):
    opts, req = inputs(tmp_path)
    plan = build_node(opts, req)
    path = Path(plan['directory'])
    (path / '.terraform').mkdir()
    (path / '.terraform' / 'retained').write_text('keep')
    build_node(opts, req)
    assert (path / '.terraform' / 'retained').read_text() == 'keep'
    assert stat.S_IMODE((path / 'compute.tf.json').stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        build_node({**opts, 'provider-backend': 's3', 's3-bucket': 'different', 's3-region': 'eu-west-1'}, req)
    unsafe = tmp_path / 'unsafe'
    unsafe.symlink_to(path, target_is_directory=True)
    with pytest.raises(ValueError):
        build_node(opts, {**req, 'workdir': str(unsafe)})


class LocalRunner:
    def __init__(self, opts, req, *, actions=None, existing=False, wrong_provider=False):
        self.opts, self.req, self.actions = opts, req, actions
        self.destroyed, self.wrong_provider = False, wrong_provider
        self.calls = []
        if existing:
            self.save_state()

    def outputs(self):
        return {'params': {'value': {'provider': 'wrong' if self.wrong_provider else self.opts['provider-compute'], 'node_id': self.req['node_id'], 'name': 'node', 'ip': '192.0.2.1', 'user': 'root', 'sudoer': 'root'}}, 'compute_identity': node_plan(self.opts, self.req)['documents']['compute.tf.json']['output']['compute_identity']}

    def state(self):
        return {'version': 4, 'serial': 1, 'lineage': 'test', 'resources': [] if self.destroyed else [{'type': 'digitalocean_droplet'}], 'outputs': {} if self.destroyed else self.outputs()}

    def save_state(self):
        path = Path(node_plan(self.opts, self.req)['directory'])
        path.mkdir(parents=True, exist_ok=True)
        (path / self.req['state_filename']).write_text(json.dumps(self.state()))

    async def __call__(self, args, cwd, env, timeout):
        self.calls.append(args)
        assert args[0] == 'tofu'
        assert not any(k.startswith('TF_VAR_keys_') for k in env)
        assert 'COLORS_PAR_SSH_PASSPHRASE' not in env
        if args[1] == 'show':
            return ProcessResult(0, json.dumps({'format_version': '1.2', 'planned_values': {}, 'resource_changes': [{'change': {'actions': self.actions or ['create']}}]}))
        if args[1] == 'apply':
            self.destroyed = self.actions == ['delete']
            self.save_state()
        if args[1] == 'state':
            return ProcessResult(0, json.dumps(self.state()))
        return ProcessResult(0, '{}')


async def execute(opts, req, runner, operation='create'):
    return await compute_node(opts, req, operation, {'COLORS_PAR_DO_TOKEN': 'secret-value', 'COLORS_PAR_SSH_PASSPHRASE': 'never-forward'}, {'runner': runner})


@pytest.mark.asyncio
async def test_compute_lifetime_does_not_own_keys(tmp_path):
    opts, req = inputs(tmp_path)
    runner = LocalRunner(opts, req)
    result = await execute(opts, req, runner)
    assert result['status'] == 'ready'
    assert 'ssh_identity_file' not in result['params']
    path = Path(result['directory'])
    assert not (path / 'ssh-key').exists()
    assert not (path / 'credentials.tfbackend.json').exists()
    assert (await execute(opts, req, runner, 'inspect'))['status'] == 'ready'
    runner.actions = ['delete']
    assert (await execute({**opts, 'compute-prevent-destroy': False}, req, runner, 'delete'))['status'] == 'destroyed'
    assert (path / 'compute.tf.json').exists()
    assert not any('ssh' in args[0] for args in runner.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['replace', 'wrong-provider', 'missing-state', 'protected', 'require-existing', 'wrong-reference'])
async def test_guards_never_apply(tmp_path, mode):
    opts, req = inputs(tmp_path)
    runner = LocalRunner(opts, req, actions=['delete', 'create'] if mode == 'replace' else None, existing=mode in ['wrong-provider', 'wrong-reference'], wrong_provider=mode == 'wrong-provider')
    operation = 'delete' if mode in ['missing-state', 'protected'] else 'create'
    if mode == 'missing-state': opts['compute-prevent-destroy'] = False
    if mode == 'require-existing': opts['compute-require-existing-state'] = True
    if mode == 'wrong-reference':
        req = {**req, 'ssh_resource': {**IDENTITY, 'reference': 'another'}, 'ssh_registration': {**req['ssh_registration'], 'ssh_resource_reference': 'another'}}
    result = await execute(opts, req, runner, operation)
    assert result['status'] == 'error'
    assert not any(args[1] == 'apply' for args in runner.calls)


@pytest.mark.asyncio
async def test_registration_create_inspect_delete_is_independent(tmp_path):
    opts, _ = inputs(tmp_path)
    req = {'name': 'access', 'workdir': str(tmp_path), 'state_filename': 'registration.tfstate', 'ssh_resource': IDENTITY}
    plan = registration_plan(opts, req)
    state = {'version': 4, 'serial': 1, 'lineage': 'registration-test', 'resources': [{'type': 'digitalocean_ssh_key'}], 'outputs': {'compute_identity': plan['documents']['compute.tf.json']['output']['compute_identity'], 'params': {'value': {'provider': 'digitalocean', 'node_id': 'registration-access', 'id': '12345'}}}}
    deleting = False
    calls = []
    async def runner(args, cwd, env, timeout):
        nonlocal state
        calls.append(args)
        assert args[0] == 'tofu'
        assert 'COLORS_PAR_SSH_PASSPHRASE' not in env
        if args[1] == 'show':
            return ProcessResult(0, json.dumps({'format_version': '1.2', 'planned_values': {}, 'resource_changes': [{'change': {'actions': ['delete' if deleting else 'create']}}]}))
        if args[1] == 'apply':
            if deleting: state = {**state, 'resources': [], 'outputs': {}}
            (Path(cwd) / req['state_filename']).write_text(json.dumps(state))
        return ProcessResult(0, json.dumps(state) if args[1] == 'state' else '{}')
    env = {'COLORS_PAR_DO_TOKEN': 'fixture-secret', 'COLORS_PAR_SSH_PASSPHRASE': 'never-forward'}
    result = await compute_registration(opts, req, 'create', env, {'runner': runner})
    assert result['status'] == 'ready'
    assert result['id'] == '12345' and result['ssh_resource_reference'] == IDENTITY['reference']
    assert (await compute_registration(opts, req, 'inspect', env, {'runner': runner}))['id'] == '12345'
    deleting = True
    assert (await compute_registration({**opts, 'compute-prevent-destroy': False}, req, 'delete', env, {'runner': runner}))['status'] == 'destroyed'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['absent', 'present', 'read-failure', 'malformed'])
@pytest.mark.parametrize('operation', ['create', 'inspect', 'delete'])
async def test_uninitialized_snapshot_requires_independent_backend_absence(tmp_path, mode, operation):
    opts, req = inputs(tmp_path)
    opts.update({'provider-backend': 'r2', 'r2-bucket': 'fixture', 'r2-endpoint': 'https://fixture.r2.cloudflarestorage.com', 'compute-prevent-destroy': False})
    delegate = LocalRunner(opts, req)
    stub = {'version': 4, 'terraform_version': '1.11.2', 'serial': 0, 'lineage': '', 'outputs': {}, 'resources': [], 'check_results': None}
    applied = False
    calls = []
    async def runner(args, cwd, env, timeout):
        nonlocal applied
        calls.append(args)
        if args[0] == 'aws':
            if mode == 'absent':
                return ProcessResult(1, '', 'An error occurred (NoSuchKey) when calling the GetObject operation: missing')
            if mode == 'read-failure':
                return ProcessResult(1, '', 'An error occurred (AccessDenied) when calling the GetObject operation: denied')
            destination = args[args.index('--key') + 2]
            Path(destination).write_text('{malformed' if mode == 'malformed' else json.dumps(stub))
            return ProcessResult(0, '{}')
        if args[1] == 'apply': applied = True
        return await delegate(args, cwd, env, timeout)
    result = await compute_node(opts, req, operation, {'COLORS_PAR_DO_TOKEN': 'fixture-token', 'COLORS_PAR_R2_ACCESS_KEY_ID': 'fixture-access', 'COLORS_PAR_R2_SECRET_ACCESS_KEY': 'fixture-secret'}, {'runner': runner})
    assert result['status'] == ('ready' if mode == 'absent' and operation == 'create' else 'error')
    assert applied == (mode == 'absent' and operation == 'create')
    assert any(args[0] == 'aws' for args in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['ok', 'missing', 'destroyed', 'replaced', 'unknown', 'identity', 'empty-ip', 'mutation', 'failure', 'failed-show', 'detached', 'errored', 'missing-prior-state'])
@pytest.mark.parametrize('provider', ['aws', 'azure', 'digitalocean', 'google', 'hcloud', 'oci', 'vultr', 'yandex'])
async def test_connection_resolution_is_live_and_read_only(tmp_path, mode, provider):
    from colors_compute.node import resolve_connection, _MACHINES
    from colors_compute.contract import registry
    opts, req = inputs(tmp_path, provider)
    plan = build_node(opts, req)
    params = {'provider': provider, 'provider_id': '123', 'node_id': req['node_id'], 'name': 'node', 'ip': '192.0.2.1', 'user': 'root', 'sudoer': 'root'}
    outputs = {'compute_identity': plan['documents']['compute.tf.json']['output']['compute_identity'], 'params': {'value': params}}
    state = {'version': 4, 'serial': 1, 'lineage': 'fixture', 'resources': [{'type': _MACHINES[provider][0], 'name': 'node', 'mode': 'managed', 'instances': [{'attributes': {_MACHINES[provider][1]: '123'}}]}], 'outputs': outputs}
    state_path = Path(plan['directory']) / req['state_filename']
    if mode != 'missing':
        state_path.write_text(json.dumps(state))
    refreshed = {'format_version': '1.2', 'planned_values': {'outputs': json.loads(json.dumps(outputs)), 'root_module': {'resources': [{'type': _MACHINES[provider][0], 'name': 'node', 'mode': 'managed', 'values': {_MACHINES[provider][1]: '123'}}]}}}
    refreshed['planned_values']['outputs']['params']['value']['ip'] = '' if mode == 'empty-ip' else '192.0.2.2'
    refreshed['prior_state'] = {'values': {'root_module': refreshed['planned_values'].pop('root_module')}}
    resources = refreshed['prior_state']['values']['root_module']['resources']
    if provider == 'google':
        resources[0]['values']['network_interface'] = [{'access_config': [{'nat_ip': '192.0.2.9' if mode == 'detached' else '192.0.2.2'}]}]
    if provider == 'azure':
        resources[0]['values'].update(id='machine', network_interface_ids=['nic'])
        resources.extend([{'type': 'azurerm_network_interface', 'name': 'node', 'mode': 'managed', 'values': {'id': 'nic', 'virtual_machine_id': 'other' if mode == 'detached' else 'machine', 'ip_configuration': [{'public_ip_address_id': 'ip'}]}}, {'type': 'azurerm_public_ip', 'name': 'node', 'mode': 'managed', 'values': {'id': 'ip', 'ip_address': '192.0.2.2'}}])
    if mode == 'destroyed':
        refreshed['prior_state']['values']['root_module']['resources'] = []
    if mode == 'replaced':
        refreshed['prior_state']['values']['root_module']['resources'][0]['values'][_MACHINES[provider][1]] = '456'
    if mode == 'identity':
        refreshed['planned_values']['outputs']['compute_identity']['value']['profile'] = 'other'
    if mode == 'unknown':
        refreshed['output_changes'] = {'params': {'after_unknown': {'ip': True}}}
    if mode == 'mutation':
        refreshed['resource_changes'] = [{'change': {'actions': ['update']}}]
    if mode == 'errored':
        refreshed['errored'] = True
    if mode == 'missing-prior-state':
        del refreshed['prior_state']
    calls = []
    async def runner(args, directory, env, timeout):
        calls.append(args)
        if args[1] == 'show' and mode == 'failed-show':
            return ProcessResult(1, '', 'invalid plan')
        if args[1] == 'plan':
            assert '-refresh-only' in args and '-lock-timeout=60s' in args
            if mode == 'failure':
                return ProcessResult(1, '', 'provider unavailable')
        return ProcessResult(0, json.dumps(refreshed if args[1] == 'show' else state), '')
    result = await resolve_connection(opts, req, {'COLORS_PAR_' + k.upper().replace('-', '_'): 'fixture-secret' for k in registry()['compute'][provider]['tofu-env']}, {'runner': runner})
    assert result['status'] == ('ready' if mode == 'ok' or mode == 'detached' and provider not in ('google', 'azure') else 'error')
    if mode == 'ok':
        assert result['params']['ip'] == '192.0.2.2'
    assert not any(c[1] == 'apply' for c in calls)
    assert not (Path(plan['directory']) / 'connection.tfplan').exists()
    if mode != 'missing':
        assert json.loads(state_path.read_text()) == state


def test_package_exports_connection_resolver():
    import colors_compute
    from colors_compute.node import resolve_connection
    assert colors_compute.resolve_connection is resolve_connection
    assert callable(colors_compute.resolve_connection)
    assert 'resolve_connection' in colors_compute.__all__
