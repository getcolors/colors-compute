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
        return deepcopy(replacements.get(value, value))
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
    required = {'node_id', 'state_filename', 'workdir', 'security'}
    if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {'network'}:
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
    key_objects = {'private': stem + '/' + node + '/ssh-key', 'public': stem + '/' + node + '/ssh-key.pub'}
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
    scoped = {**deepcopy(opts), 'profile': profile + '-' + node}
    recipe = recipes[provider]
    req = {'node_id': node, 'name': scoped['profile'], 'key': {'mode': 'managed', 'public_key': 'colors-public-key-placeholder'},
           'network': deepcopy(request.get('network', {'mode': recipe['network_mode']})), 'security': deepcopy(request['security'])}
    shared_docs = provider_request(scoped, 'shared', req)['documents']
    shared = {}
    for document in shared_docs.values():
        for name, output in document.get('output', {}).items():
            shared[name] = deepcopy(output['value'])
    replacements = {'colors-public-key-placeholder': '${tls_private_key.machine.public_key_openssh}'}
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
    local_keys = backend == 'local'
    if local_keys and any(str(name).startswith('ssh-s3-') for name in opts):
        raise ValueError('local backend owns local keys; SSH S3 settings are unsupported')
    required_providers = merged.setdefault('terraform', {}).setdefault('required_providers', {})
    required_providers['tls'] = {'source': 'hashicorp/tls', 'version': '4.1.0'}
    merged.setdefault('output', {})['compute_identity'] = {'value': {'profile': profile, 'node_id': node, 'state_filename': filename, 'provider': provider}}
    merged['output']['ssh_public_key_fingerprint'] = {'value': '${tls_private_key.machine.public_key_fingerprint_sha256}'}
    resources = merged.setdefault('resource', {})
    dependencies = ['tls_private_key.machine'] if local_keys else ['aws_s3_object.ssh_private', 'aws_s3_object.ssh_public']
    for instances in resources.values():
        for resource in instances.values():
            resource.setdefault('depends_on', []).extend(dependencies)
    resources['tls_private_key'] = {'machine': {'algorithm': 'ED25519'}}
    if local_keys:
        key_objects = {}
        merged['output']['ssh_private_key'] = {'value': '${tls_private_key.machine.private_key_openssh}', 'sensitive': True}
        merged['output']['ssh_public_key'] = {'value': '${tls_private_key.machine.public_key_openssh}'}
    else:
        settings = _key_settings(opts, state_key)
        keys_provider = {'alias': 'keys', 'region': settings['region'],
                         'access_key': '${var.keys_access_key}', 'secret_key': '${var.keys_secret_key}'}
        for field in ('endpoints', 'skip_credentials_validation', 'skip_metadata_api_check', 'skip_region_validation', 'skip_requesting_account_id'):
            if field in settings:
                keys_provider[field] = deepcopy(settings[field])
        if 'use_path_style' in settings:
            keys_provider['s3_use_path_style'] = settings['use_path_style']
        providers = merged.setdefault('provider', {})
        providers['aws'] = ([providers['aws']] if 'aws' in providers else []) + [keys_provider]
        required_providers.setdefault('aws', {'source': 'hashicorp/aws', 'version': '6.31.0'})
        merged['variable'] = {name: {'type': 'string', 'sensitive': True, 'default': None} for name in ('keys_access_key', 'keys_secret_key')}
        resources['aws_s3_object'] = {
            'ssh_' + kind: {'provider': 'aws.keys', 'bucket': settings['bucket'], 'key': key_objects[kind],
                           'force_destroy': False, 'content': '${tls_private_key.machine.' + ('private_key_openssh' if kind == 'private' else 'public_key_openssh') + '}',
                           **({'server_side_encryption': 'AES256'} if backend == 's3' or backend == 'gcs' and not opts.get('ssh-s3-endpoint') else {})}
            for kind in ('private', 'public')}
    backend_document = ({'terraform': {'backend': {'local': {'path': str(Path(directory) / filename)}}}}
                        if backend == 'local' else backend_plan(opts, state_key)['config'])
    return {'status': 'planned', 'directory': directory, 'state_key': state_key, 'key_objects': key_objects,
            'documents': {'compute.tf.json': merged, 'backend.tf.json': backend_document}}


def _key_settings(opts, state_key):
    from urllib.parse import urlparse
    if opts.get('provider-backend') in ('s3', 'r2', 'oci'):
        if any(name in opts for name in ('ssh-s3-bucket', 'ssh-s3-region', 'ssh-s3-endpoint')):
            raise ValueError('remote key storage is bound to the state backend')
        settings = backend_plan(opts, state_key)['config']['terraform']['backend']['s3']
    else:
        settings = {'bucket': opts.get('ssh-s3-bucket'), 'region': opts.get('ssh-s3-region')}
        if opts.get('ssh-s3-endpoint') is not None:
            settings.update(endpoints={'s3': opts['ssh-s3-endpoint']}, use_path_style=True,
                            skip_credentials_validation=True, skip_metadata_api_check=True,
                            skip_region_validation=True, skip_requesting_account_id=True)
    if any(not isinstance(settings.get(name), str) or not settings[name].strip()
           or settings[name].strip().upper() == 'REPLACE_ME' for name in ('bucket', 'region')):
        raise ValueError('remote SSH storage bucket and region are required')
    endpoint = settings.get('endpoints', {}).get('s3')
    if endpoint is not None:
        if not isinstance(endpoint, str):
            raise ValueError('invalid SSH storage endpoint')
        parsed = urlparse(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('invalid SSH storage endpoint')
    return settings


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


def build_node(opts, request):
    plan = node_plan(opts, request)
    _directory(plan['directory'])
    existing = Path(plan['directory']) / 'compute.tf.json'
    if existing.exists():
        previous = json.loads(_read_file(existing))
        provider = previous.get('output', {}).get('params', {}).get('value', {}).get('provider')
        if provider != opts['provider-compute']:
            raise ValueError('provider change requires a distinct node identity')
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
        if operation not in ('create', 'delete', 'inspect', 'prepare-access', 'build'):
            raise ValueError('invalid node operation')
        if operation == 'build':
            node_plan(opts, request)
            stage = 'build'
            return build_node(opts, request)
        if operation == 'delete' and opts.get('compute-prevent-destroy', True):
            raise ValueError('compute destruction is protected')
        plan = node_plan(opts, request)
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
        if opts['provider-backend'] == 'gcs':
            key_credentials = {}
            for name, field in (('COLORS_PAR_SSH_S3_ACCESS_KEY_ID', 'access_key'), ('COLORS_PAR_SSH_S3_SECRET_ACCESS_KEY', 'secret_key')):
                if source.get(name):
                    key_credentials[field] = source[name]
            if key_credentials and set(key_credentials) != {'access_key', 'secret_key'}:
                raise ValueError('incomplete SSH storage credentials')
        if key_credentials:
            child['TF_VAR_keys_access_key'] = key_credentials['access_key']
            child['TF_VAR_keys_secret_key'] = key_credentials['secret_key']
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
        build_node(opts, request)
        credential_path = Path(directory) / 'credentials.tfbackend.json'
        try:
            _write(credential_path, json.dumps(credentials).encode())
            await run(['tofu', 'init', '-input=false', '-no-color', '-reconfigure', '-backend-config=' + str(credential_path)])
        finally:
            credential_path.unlink(missing_ok=True)
        stage = 'state'
        settings = None if opts['provider-backend'] == 'local' else _key_settings(opts, plan['state_key'])
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
            if not present and opts['provider-backend'] == 'local' and any((Path(directory) / name).exists() or (Path(directory) / name).is_symlink() for name in ('ssh-key', 'ssh-key.pub')):
                raise ValueError('local state is missing beside existing key copies; recover state')
            if not present and operation != 'create':
                raise ValueError('state missing; explicit recovery required')
            if not present and opts.get('compute-require-existing-state', False):
                raise ValueError('existing state required')
            if operation == 'create':
                for kind, key in plan['key_objects'].items():
                    owned = [resource for resource in existing if resource.get('mode') == 'managed' and resource.get('type') == 'aws_s3_object' and resource.get('name') == 'ssh_' + kind]
                    if owned:
                        if len(owned) != 1 or len(owned[0].get('instances', [])) != 1:
                            raise ValueError('ambiguous remote key ownership')
                        attrs = owned[0]['instances'][0].get('attributes', {})
                        if attrs.get('bucket') != settings['bucket'] or attrs.get('key') != key:
                            raise ValueError('remote key resource identity mismatch')
                    elif await get_object(key, observed_path):
                        raise ValueError('remote key ownership is missing')
        finally:
            observed_path.unlink(missing_ok=True)
        if operation == 'inspect' and not existing and not state['outputs']:
            return {'status': 'destroyed', 'directory': directory}
        if operation in ('inspect', 'prepare-access') and not existing:
            raise ValueError('node state is absent')
        if operation == 'delete' and not existing and not state['outputs']:
            stage = 'cleanup'
            for name in ('ssh-key', 'ssh-key.pub'):
                (Path(directory) / name).unlink(missing_ok=True)
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
            for name in ('ssh-key', 'ssh-key.pub'):
                (Path(directory) / name).unlink(missing_ok=True)
            return {'status': 'destroyed', 'directory': directory}
        state_text = await run(['tofu', 'state', 'pull'])
        params = _params(state_text)
        state_outputs = json.loads(state_text)['outputs']
        if state_outputs.get('compute_identity', {}).get('value') != expected_identity:
            raise ValueError('node state identity mismatch')
        if params.get('provider') != opts['provider-compute'] or params.get('node_id') != request['node_id']:
            raise ValueError('node state identity mismatch')
        if operation != 'inspect':
            stage = 'access'
            settings = None if opts['provider-backend'] == 'local' else _key_settings(opts, plan['state_key'])
            key_env = {key: value for key, value in child.items() if not key.startswith(('TF_', 'TOFU_'))}
            if key_credentials:
                for name in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_SESSION_TOKEN'):
                    key_env.pop(name, None)
                key_env['AWS_ACCESS_KEY_ID'] = key_credentials['access_key']
                key_env['AWS_SECRET_ACCESS_KEY'] = key_credentials['secret_key']
            paths = []
            try:
                for kind, filename in (('private', 'ssh-key'), ('public', 'ssh-key.pub')):
                    path = Path(directory) / (filename + '.download')
                    path.unlink(missing_ok=True)
                    _write(path, b'')
                    paths.append(path)
                    if opts['provider-backend'] == 'local':
                        output = state_outputs.get('ssh_' + kind + '_key', {})
                        value = output.get('value')
                        if not isinstance(value, str) or not value.strip() or kind == 'private' and output.get('sensitive') is not True:
                            raise ValueError('local key outputs are missing or invalid')
                        if kind == 'private':
                            diagnostic_secrets.add(value)
                        _write(path, value.encode())
                    else:
                        args = ['aws', 's3api', 'get-object', '--bucket', settings['bucket'], '--key', plan['key_objects'][kind], str(path), '--region', settings['region'], '--no-cli-pager']
                        if 'endpoints' in settings:
                            args += ['--endpoint-url', settings['endpoints']['s3']]
                        await run(args, key_env)
                    path.chmod(0o600)
                diagnostic_secrets.add(_read_file(paths[0]))
                public = await run(['ssh-keygen', '-y', '-f', str(paths[0])], key_env)
                if public.split()[:2] != paths[1].read_text().split()[:2]:
                    raise ValueError('remote keypair mismatch')
                fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(public.split()[1], validate=True)).digest()).decode().rstrip('=')
                if fingerprint != state_outputs.get('ssh_public_key_fingerprint', {}).get('value'):
                    raise ValueError('remote key fingerprint mismatch')
                for path, filename in zip(paths, ('ssh-key', 'ssh-key.pub')):
                    os.replace(path, Path(directory) / filename)
            except BaseException:
                for name in ('ssh-key', 'ssh-key.pub', 'ssh-key.download', 'ssh-key.pub.download'):
                    (Path(directory) / name).unlink(missing_ok=True)
                raise
            params['ssh_identity_file'] = str(Path(directory) / 'ssh-key')
        stage = 'state'
        templates = json.loads(files('colors_compute').joinpath('templates.json').read_text())[opts['provider-compute']]
        allowed = {'ssh_identity_file'}
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
