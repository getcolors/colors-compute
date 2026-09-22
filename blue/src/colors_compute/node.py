"""One persistent SDK compute unit; OpenTofu owns its infrastructure and remote keys."""
import json
import os
from pathlib import Path
import re
import stat
from importlib.resources import files

from ._copy import deepcopy
from .backend import _run, _params
from .contract import _safe, _local_path, registry
import base64
import hashlib
from .provider_request import provider_request
from .rendering import backend_plan
from .diagnostics import NodeError, command_metadata, credential_values, failure


def _valid_plan(output, operation):
    try:
        plan = json.loads(output, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        return False
    if not isinstance(plan, dict) or not isinstance(plan.get('format_version'), str) or not plan['format_version'].strip() or not isinstance(plan.get('planned_values'), dict):
        return False
    changes = plan.get('resource_changes', [])
    if not isinstance(changes, list):
        return False
    permitted = {'no-op', 'read', 'create', 'update'} if operation == 'create' else {'no-op', 'read', 'delete'}
    for resource in changes:
        if not isinstance(resource, dict) or not isinstance(resource.get('change'), dict):
            return False
        actions = resource['change'].get('actions')
        if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], str) or actions[0] not in permitted:
            return False
    return True


def _replace(value, replacements):
    if isinstance(value, str):
        return _replace(replacements[value], replacements) if value in replacements else value
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def _merge(target, source):
    for key, value in source.items():
        if key not in target:
            target[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _merge(target[key], value)
        elif target[key] != value:
            raise ValueError('conflicting node template declaration')


_REGISTRATIONS = {'aws': 'aws_key_pair', 'digitalocean': 'digitalocean_ssh_key', 'hcloud': 'hcloud_ssh_key', 'vultr': 'vultr_ssh_key'}


def _public_identity(value):
    if not isinstance(value, dict) or set(value) - {'reference', 'public_key', 'fingerprint', 'status'}:
        raise ValueError('invalid public SSH resource')
    if not isinstance(value.get('reference'), str) or not value['reference'].strip():
        raise ValueError('SSH resource reference required')
    public = value.get('public_key', '').split()
    if len(public) != 2 or public[0] != 'ssh-ed25519':
        raise ValueError('ED25519 public identity required')
    try:
        blob = base64.b64decode(public[1], validate=True)
        if len(blob) != 51 or blob[:19] != bytes.fromhex('0000000b7373682d6564323535313900000020'):
            raise ValueError()
    except Exception:
        raise ValueError('invalid ED25519 public identity') from None
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    if value.get('fingerprint') != fingerprint:
        raise ValueError('SSH fingerprint mismatch')
    return value


def _registration_identity(provider, request, identity):
    value = request.get('ssh_registration')
    if provider not in _REGISTRATIONS:
        if value is not None:
            raise ValueError('provider consumes public SSH identity directly')
        return None
    if not isinstance(value, dict) or value.get('status') != 'ready' or value.get('provider') != provider or value.get('ssh_resource_reference') != identity['reference'] or value.get('fingerprint') != identity['fingerprint'] or not isinstance(value.get('id'), str) or not value['id'].strip() or not isinstance(value.get('reference'), str) or not value['reference'].strip():
        raise ValueError('matching SSH registration required')
    return value


def node_plan(opts, request):
    """Render one node without credentials, filesystem changes, or topology expansion."""
    opts, request = deepcopy(opts), deepcopy(request)
    def literals(value):
        if isinstance(value, str) and any(token in value for token in ('${', '%{', '\x00')):
            raise ValueError('invalid compute literal')
        if isinstance(value, dict):
            for key, item in value.items():
                literals(key)
                literals(item)
        elif isinstance(value, list):
            for item in value:
                literals(item)
    literals(opts)
    literals(request)
    if 'compute-role-settings' in opts:
        raise ValueError('topology options are outside the compute node API')
    required = {'node_id', 'state_filename', 'workdir', 'security', 'ssh_resource'}
    if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {'network', 'ssh_registration'}:
        raise ValueError('invalid single-node request')
    profile, node = opts.get('profile'), request['node_id']
    if not _safe(profile) or not _safe(node):
        raise ValueError('invalid profile or node identifier')
    filename = request['state_filename']
    if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.tfstate', filename):
        raise ValueError('invalid node state filename')
    if not _local_path(request['workdir']):
        raise ValueError('SDK workdir must be absolute')
    backend = opts.get('provider-backend')
    prefix = opts.get('s3-prefix', '')
    if not isinstance(prefix, str):
        raise ValueError('invalid S3 prefix')
    if prefix and any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', part) or part in ('.', '..') for part in prefix.split('/')):
        raise ValueError('invalid S3 prefix')
    stem = '/'.join(part for part in (prefix, profile) if part)
    state_key = stem + '/' + filename
    key_objects = {}
    directory = str(Path(request['workdir']) / profile / node)
    if 'compute-require-existing-state' in opts and type(opts['compute-require-existing-state']) is not bool:
        raise ValueError('compute-require-existing-state must be a boolean')
    provider = opts.get('provider-compute')
    recipes = json.loads(files('colors_compute').joinpath('provider-recipes.json').read_text())
    if provider not in recipes:
        raise ValueError('unsupported compute provider')
    entry = registry()['compute'][provider]
    external_settings = [entry['ssh-setting'], *entry.get('ssh-aliases', {}), 'ssh-key-path', 'ssh-private-key-path', 'ssh-public-key-path', provider + '-ssh-private-key']
    if any(key in opts for key in external_settings):
        raise ValueError('external SSH keys are outside the single-node contract')
    if not _safe(profile + '-' + node):
        raise ValueError('combined profile and node identifier exceeds 63 characters')
    identity = _public_identity(request['ssh_resource'])
    registration = _registration_identity(provider, request, identity)
    scoped = {**deepcopy(opts), 'profile': profile + '-' + node}
    recipe = recipes[provider]
    req = {'node_id': node, 'name': scoped['profile'], 'key': {'mode': 'managed', 'public_key': 'colors-public-key-placeholder'},
           'network': deepcopy(request.get('network', {'mode': recipe['network_mode']})), 'security': deepcopy(request['security'])}
    shared_docs = provider_request(scoped, 'shared', req)['documents']
    shared = {}
    for document in shared_docs.values():
        for name, output in document.get('output', {}).items():
            shared[name] = deepcopy(output['value'])
    replacements = {'colors-public-key-placeholder': identity['public_key']}
    if registration:
        kind = _REGISTRATIONS[provider]
        replacements.update({'${' + kind + '.machine.' + field + '}': registration['id'] for field in ('id', 'key_name')})
    def placeholders(value):
        if isinstance(value, str) and ('${' in value or '%{' in value):
            token = 'colors-shared-reference-' + str(len(replacements))
            replacements[token] = value
            return token
        if isinstance(value, dict):
            return {key: placeholders(item) for key, item in value.items()}
        if isinstance(value, list):
            return [placeholders(item) for item in value]
        return value
    node_docs = provider_request(scoped, 'node', req, placeholders(shared))['documents']
    merged = {}
    for document in shared_docs.values():
        _merge(merged, {key: value for key, value in document.items() if key != 'output'})
    for document in node_docs.values():
        _merge(merged, document)
    merged = _replace(merged, replacements)
    if registration:
        merged.get('resource', {}).pop(_REGISTRATIONS[provider], None)
    merged.setdefault('output', {})['compute_identity'] = {'value': {'profile': profile, 'node_id': node, 'state_filename': filename, 'provider': provider, 'ssh_resource_reference': identity['reference'], 'ssh_fingerprint': identity['fingerprint']}}
    backend_document = ({'terraform': {'backend': {'local': {'path': str(Path(directory) / filename)}}}}
                        if backend == 'local' else backend_plan(opts, state_key)['config'])
    return {'status': 'planned', 'directory': directory, 'state_key': state_key, 'key_objects': key_objects,
            'documents': {'compute.tf.json': merged, 'backend.tf.json': backend_document}}


def _directory(path):
    path = Path(path)
    for parent in reversed((path, *path.parents)):
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError('unsafe SDK directory')
    path.chmod(0o700)


def _write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise ValueError('unsafe SDK file')
    os.ftruncate(fd, 0)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def _read_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('unsafe SDK file')
    with os.fdopen(fd, 'r') as stream:
        return stream.read()


def build_node(opts, request, _planner=node_plan):
    plan = _planner(opts, request)
    _directory(plan['directory'])
    existing = Path(plan['directory']) / 'compute.tf.json'
    if existing.exists():
        previous = json.loads(_read_file(existing))
        provider = previous.get('output', {}).get('compute_identity', {}).get('value', {}).get('provider')
        if previous.get('output', {}).get('compute_identity') != plan['documents']['compute.tf.json']['output']['compute_identity']:
            raise ValueError('compute identity change requires a distinct directory')
    backend_path = Path(plan['directory']) / 'backend.tf.json'
    if backend_path.exists() and json.loads(_read_file(backend_path)) != plan['documents']['backend.tf.json']:
        raise ValueError('node backend identity changed; explicit migration required')
    for filename, document in plan['documents'].items():
        _write(Path(plan['directory']) / filename, json.dumps(document, indent=2).encode())
    return {**plan, 'status': 'built'}


async def compute_node(opts, request, operation='create', environment=None, dependencies=None):
    """Run one node lifecycle with bounded, sanitized failure diagnostics."""
    stage, changes = 'validate', 'none'
    diagnostic_secrets = set()
    try:
        source = dict(os.environ if environment is None else environment)
        diagnostic_secrets = credential_values(source, opts)
        planner = (dependencies or {}).get('_planner', node_plan)
        if operation not in ('create', 'delete', 'inspect', 'build'):
            raise ValueError('invalid node operation')
        if operation == 'build':
            planner(opts, request)
            stage = 'build'
            return build_node(opts, request, planner)
        if operation == 'delete' and opts.get('compute-prevent-destroy', True):
            raise ValueError('compute destruction is protected')
        plan = planner(opts, request)
        directory = plan['directory']
        stage = 'build'
        _directory(directory)
        stage = 'credentials'
        child = {key: value for key, value in source.items() if not key.startswith(('TF_', 'TOFU_', 'COLORS_PAR_'))}
        child.update(TF_IN_AUTOMATION='1', TF_INPUT='0', TF_WORKSPACE='default')
        for key, variable in registry()['compute'][opts['provider-compute']]['tofu-env'].items():
            value = source.get('COLORS_PAR_' + key.upper().replace('-', '_'))
            if not value:
                raise NodeError('missing_credentials', credential='COLORS_PAR_' + key.upper().replace('-', '_'))
            child[variable] = value
        backend = ({'credential_bindings': {}, 'config': plan['documents']['backend.tf.json']}
                   if opts['provider-backend'] == 'local' else backend_plan(opts, plan['state_key']))
        credentials = {}
        for variable, field in backend['credential_bindings'].items():
            if not source.get(variable):
                raise NodeError('missing_credentials', credential=variable)
            credentials[field] = source[variable]
        key_credentials = dict(credentials)
        execute = (dependencies or {}).get('runner', _run)
        async def invoke(args, env, timeout):
            nonlocal changes
            if args[:2] == ['tofu', 'apply']:
                changes = 'possible'
            try:
                return await execute(args, directory, env, timeout)
            except OSError:
                raise NodeError('command_failed', **command_metadata(args, env, directory)) from None

        async def run(args, env=None, timeout=120000):
            nonlocal stage
            if args[0] == 'tofu':
                stage = {'init': 'init', 'state': 'state', 'plan': 'plan', 'show': 'plan-validation', 'apply': 'apply'}.get(args[1], stage)
            elif args[0] == 'ssh-keygen':
                stage = 'access'

            for filename in ('approved.tfplan', '.terraform.lock.hcl', request['state_filename'], request['state_filename'] + '.backup', '.terraform/terraform.tfstate'):
                candidate = Path(directory) / filename
                if candidate.is_symlink() or candidate.exists() and not stat.S_ISREG(candidate.lstat().st_mode):
                    raise ValueError('unsafe OpenTofu working file')
            data_directory = Path(directory) / '.terraform'
            if data_directory.is_symlink() or data_directory.exists() and not stat.S_ISDIR(data_directory.lstat().st_mode):
                raise ValueError('unsafe OpenTofu data directory')
            command_env = child if env is None else env
            result = await invoke(args, command_env, timeout)
            if result.exit != 0:
                code = 'state_unreadable' if stage == 'state' else 'key_access_failed' if stage == 'access' else 'command_failed'
                raise NodeError(code, **command_metadata(args, command_env, directory, result))
            for filename in ('approved.tfplan', '.terraform.lock.hcl', request['state_filename'], request['state_filename'] + '.backup'):
                candidate = Path(directory) / filename
                if candidate.exists():
                    if not stat.S_ISREG(candidate.lstat().st_mode):
                        raise ValueError('unsafe OpenTofu working file')
                    candidate.chmod(0o600)
            if (Path(directory) / '.terraform').exists():
                if not stat.S_ISDIR((Path(directory) / '.terraform').lstat().st_mode):
                    raise ValueError('unsafe OpenTofu data directory')
                (Path(directory) / '.terraform').chmod(0o700)
            return result.out
        stage = 'build'
        # Validate the old configuration before replacing templates. A provider
        # switch may not hide resources that remain owned by the existing state.
        backend_path = Path(directory) / 'backend.tf.json'
        if backend_path.exists():
            old_backend = json.loads(_read_file(backend_path))
            if old_backend != plan['documents']['backend.tf.json']:
                raise ValueError('node backend identity changed; explicit migration required')
        build_node(opts, request, planner)
        credential_path = Path(directory) / 'credentials.tfbackend.json'
        try:
            _write(credential_path, json.dumps(credentials).encode())
            await run(['tofu', 'init', '-input=false', '-no-color', '-reconfigure', '-backend-config=' + str(credential_path)])
        finally:
            credential_path.unlink(missing_ok=True)
        stage = 'state'
        settings = backend_plan(opts, plan['state_key'])['config']['terraform']['backend']['s3'] if opts['provider-backend'] in ('s3', 'r2', 'oci') else None
        key_env = {key: value for key, value in child.items() if not key.startswith(('TF_', 'TOFU_'))}
        if key_credentials:
            for name in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_SESSION_TOKEN'):
                key_env.pop(name, None)
            key_env.update(AWS_ACCESS_KEY_ID=key_credentials['access_key'], AWS_SECRET_ACCESS_KEY=key_credentials['secret_key'])
        async def get_object(key, destination):
            _write(destination, b'')
            args = ['aws', 's3api', 'get-object', '--bucket', settings['bucket'], '--key', key,
                    str(destination), '--region', settings['region'], '--no-cli-pager']
            if 'endpoints' in settings:
                args += ['--endpoint-url', settings['endpoints']['s3']]
            result = await invoke(args, key_env, 120000)
            if result.exit == 0:
                return True
            if re.match(r'\s*(?:aws: \[ERROR\]: )?An error occurred \(NoSuchKey\) when calling the GetObject operation(?: \(reached max retries: [0-9]+\))?:', result.err):
                return False
            raise NodeError('state_unreadable', **command_metadata(args, key_env, directory, result))
        observed_path = Path(directory) / '.observed.tfstate'
        try:
            if opts['provider-backend'] in ('s3', 'r2', 'oci'):
                present = await get_object(plan['state_key'], observed_path)
                state_text = _read_file(observed_path) if present else None
            elif opts['provider-backend'] == 'local':
                state_path = Path(plan['documents']['backend.tf.json']['terraform']['backend']['local']['path'])
                state_text = _read_file(state_path) if state_path.exists() else None
                present = state_text is not None
            else:
                _write(observed_path, b'')
                url = 'gs://' + opts['gcs-bucket'] + '/' + plan['state_key'] + '/default.tfstate'
                gcs_args = ['gcloud', 'storage', 'cp', url, str(observed_path)]
                result = await invoke(gcs_args, child, 120000)
                present = result.exit == 0
                if not present:
                    if 'No URLs matched' not in result.err:
                        raise NodeError('state_unreadable', **command_metadata(gcs_args, child, directory, result))
                    bucket = json.loads(await run(['gcloud', 'storage', 'buckets', 'describe', 'gs://' + opts['gcs-bucket'], '--format=json']))
                    if bucket.get('name') != opts['gcs-bucket']:
                        raise ValueError('GCS bucket identity mismatch')
                state_text = _read_file(observed_path) if present else None
            prior = _params(state_text) if present else {}
            state = json.loads(state_text) if present else {'resources': [], 'outputs': {}}
            existing = state['resources']
            if not existing and state['outputs']:
                raise ValueError('state with leftover outputs requires explicit recovery')
            identity = state['outputs'].get('compute_identity', {}).get('value')
            expected_identity = plan['documents']['compute.tf.json']['output']['compute_identity']['value']
            if existing and (identity != expected_identity or prior.get('provider') != opts['provider-compute']):
                raise ValueError('provider or unit change requires explicit state recovery')
            if not present and operation != 'create':
                raise ValueError('state missing; explicit recovery required')
            if not present and opts.get('compute-require-existing-state', False):
                raise ValueError('existing state required')
        finally:
            observed_path.unlink(missing_ok=True)
        if operation == 'inspect' and not existing and not state['outputs']:
            return {'status': 'destroyed', 'directory': directory}
        if operation in ('inspect', 'prepare-access') and not existing:
            raise ValueError('node state is absent')
        if operation == 'delete' and not existing and not state['outputs']:
            stage = 'cleanup'
            return {'status': 'destroyed', 'directory': directory}
        if operation in ('create', 'delete'):
            args = ['tofu', 'plan', '-input=false', '-no-color', '-out=approved.tfplan']
            if operation == 'delete':
                args.append('-destroy')
            await run(args, timeout=1800000)
            if not _valid_plan(await run(['tofu', 'show', '-json', 'approved.tfplan']), operation):
                raise ValueError('unsafe node plan')
            await run(['tofu', 'apply', '-input=false', '-no-color', 'approved.tfplan'], timeout=1800000)
        if operation == 'delete':
            after_text = await run(['tofu', 'state', 'pull'])
            _params(after_text)
            after = json.loads(after_text)
            if after['resources'] or after['outputs']:
                raise ValueError('node resources remain')
            stage = 'cleanup'
            return {'status': 'destroyed', 'directory': directory}
        state_text = await run(['tofu', 'state', 'pull'])
        params = _params(state_text)
        state_outputs = json.loads(state_text)['outputs']
        if state_outputs.get('compute_identity', {}).get('value') != expected_identity:
            raise ValueError('node state identity mismatch')
        if params.get('provider') != opts['provider-compute'] or params.get('node_id') != request['node_id']:
            raise ValueError('node state identity mismatch')
        stage = 'state'
        templates = json.loads(files('colors_compute').joinpath('templates.json').read_text())[opts['provider-compute']]
        allowed = {'provider', 'node_id', 'id'} if (dependencies or {}).get('_registration') else set()
        for stage_name, documents in templates.items():
            if stage_name.startswith('node'):
                for document in documents.values():
                    allowed.update(document.get('output', {}).get('params', {}).get('value', {}))
        if set(params) - allowed:
            raise ValueError('unexpected node outputs')
        encoded = json.dumps(params)
        secrets = list(credentials.values()) + list(key_credentials.values()) + [value for key, value in source.items() if key.startswith('COLORS_PAR_') and isinstance(value, str) and value]
        local_private = state_outputs.get('ssh_private_key', {}).get('value')
        if isinstance(local_private, str) and local_private:
            secrets.append(local_private)
        if '-----BEGIN ' in encoded or any(field in encoded.lower() for field in ('\"private_key\"', '\"private_key_openssh\"', '\"secret_key\"', '\"access_key\"')) or any(secret in encoded for secret in secrets):
            raise ValueError('sensitive node output')
        return {'status': 'ready', 'directory': directory, 'params': params}
    except Exception as error:
        return failure(error, stage, changes, diagnostic_secrets)


def _registration_request(request):
    if set(request) != {'name', 'workdir', 'state_filename', 'ssh_resource'} or not _safe(request['name']):
        raise ValueError('invalid SSH registration request')
    return {'node_id': 'registration-' + request['name'], 'workdir': request['workdir'], 'state_filename': request['state_filename'], 'ssh_resource': request['ssh_resource']}


def _registration_plan(opts, internal):
    provider = opts['provider-compute']
    if provider not in _REGISTRATIONS:
        raise ValueError('provider needs no SSH registration')
    identity = _public_identity(internal['ssh_resource'])
    name = internal['node_id']
    if not _safe(opts.get('profile')) or not _safe(name) or not _safe(opts['profile'] + '-' + name) or not _local_path(internal['workdir']) or not isinstance(internal['state_filename'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.tfstate', internal['state_filename']):
        raise ValueError('invalid registration identity')
    if any(token in json.dumps([opts, internal]) for token in ('${', '%{', '\\u0000')):
        raise ValueError('invalid registration literal')
    prefix = opts.get('s3-prefix', '')
    if not isinstance(prefix, str) or prefix and any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', part) for part in prefix.split('/')):
        raise ValueError('invalid S3 prefix')
    state_key = '/'.join(part for part in (prefix, opts['profile'], internal['state_filename']) if part)
    directory = str(Path(internal['workdir']) / opts['profile'] / name)
    templates = json.loads(files('colors_compute').joinpath('templates.json').read_text())[provider]['shared-keygen']
    root = {}
    for document in templates.values():
        _merge(root, {k: v for k, v in document.items() if k in ('terraform', 'provider')})
    if provider == 'aws':
        if not isinstance(opts.get('aws-region'), str) or not opts['aws-region'].strip():
            raise ValueError('AWS region required')
        root['provider']['aws']['region'] = opts['aws-region']
    root['output'] = {'compute_identity': {'value': {'profile': opts['profile'], 'node_id': name, 'state_filename': internal['state_filename'], 'provider': provider, 'ssh_resource_reference': identity['reference'], 'ssh_fingerprint': identity['fingerprint']}}}
    backend = {'terraform': {'backend': {'local': {'path': str(Path(directory) / internal['state_filename'])}}}} if opts['provider-backend'] == 'local' else backend_plan(opts, state_key)['config']
    plan = {'status': 'planned', 'directory': directory, 'state_key': state_key, 'key_objects': {}, 'documents': {'compute.tf.json': root, 'backend.tf.json': backend}}
    resource = {'key_name' if provider == 'aws' else 'name': opts['profile'] + '-' + name, 'ssh_key' if provider == 'vultr' else 'public_key': identity['public_key'], 'lifecycle': {'prevent_destroy': opts.get('compute-prevent-destroy', True)}}
    reference = plan['state_key']
    root['resource'] = {_REGISTRATIONS[provider]: {'machine': resource}}
    root.pop('data', None)
    root.pop('locals', None)
    root['output'] = {'compute_identity': {'value': {**root['output']['compute_identity']['value'], 'kind': 'ssh-registration', 'provider_scope': opts.get('aws-region') if provider == 'aws' else provider}}, 'params': {'value': {'provider': provider, 'node_id': name, 'id': '${tostring(' + _REGISTRATIONS[provider] + '.machine.' + ('key_name' if provider == 'aws' else 'id') + ')}'}}}
    return plan


def registration_plan(opts, request):
    return _registration_plan(opts, _registration_request(request))


def build_registration(opts, request):
    return build_node(opts, _registration_request(request), _registration_plan)


async def compute_registration(opts, request, operation='create', environment=None, dependencies=None):
    internal = _registration_request(request)
    result = await compute_node(opts, internal, operation, environment, {**(dependencies or {}), '_planner': _registration_plan, '_registration': True})
    if result['status'] == 'ready':
        identity = _public_identity(request['ssh_resource'])
        result = {**result, 'reference': registration_plan(opts, request)['state_key'], 'provider': opts['provider-compute'], 'ssh_resource_reference': identity['reference'], 'fingerprint': identity['fingerprint'], 'id': result['params']['id']}
        del result['params']
    return result
