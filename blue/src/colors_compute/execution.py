"""Guarded per-state execution; only a locked coordinator may call converge_state."""
import json
import asyncio
import os
from pathlib import Path
import re
import tempfile
from importlib.resources import files

from .backend import _run, _params, _outputs
from .contract import _safe, _missing, registry
from .journal import _environment, _service_error, _etag, _write, _contains_secret
from .rendering import backend_plan


def _state_key(opts, key):
    profile = opts.get('profile')
    if not _safe(profile) or not isinstance(key, str):
        return False
    if key in (profile + '/compute/shared.tfstate', profile + '/compute/managed-kubernetes.tfstate'):
        return True
    prefix = profile + '/compute/nodes/'
    return key.startswith(prefix) and key.endswith('.tfstate') and _safe(key[len(prefix):-8])


async def state_presence(opts, state_key, environment=None, runner=None, legacy=False):
    """Observe only approved remote state keys; failures never prove absence."""
    try:
        legacy_key = legacy is True and _safe(opts.get('profile')) and isinstance(state_key, str) and re.fullmatch(re.escape(opts['profile']) + r'/[A-Za-z0-9][A-Za-z0-9_-]{0,62}\.tfstate', state_key)
        if not _state_key(opts, state_key) and not legacy_key:
            return {'status': 'error'}
        backend = backend_plan(opts, state_key)['config']['terraform']['backend']
        if opts['provider-backend'] == 'gcs':
            from .gcs import gcs_client, object_path
            request = await gcs_client(environment, runner)
            metadata = await request('GET', object_path(opts['gcs-bucket'], state_key + '/default.tfstate'))
            return {'status': 'absent' if metadata is None else 'present'}
        settings = backend['s3']
        source = dict(os.environ if environment is None else environment)
        with tempfile.TemporaryDirectory(prefix='colors-compute-presence-') as directory:
            path = Path(directory)
            os.chmod(path, 0o700)
            child = _environment(opts, source, path)
            body = path / 'state.json'
            _write(body, b'')
            command = ['aws', 's3api', 'get-object', '--bucket', settings['bucket'], '--key', state_key,
                       str(body), '--region', settings['region'], '--output', 'json', '--no-cli-pager']
            secrets = [source[key] for key in (f"COLORS_PAR_{opts['provider-backend'].upper()}_ACCESS_KEY_ID", f"COLORS_PAR_{opts['provider-backend'].upper()}_SECRET_ACCESS_KEY")] if opts['provider-backend'] in ('r2', 'oci') else []
            if opts['provider-backend'] in ('r2', 'oci'):
                command.extend(['--endpoint-url', settings['endpoints']['s3']])
            if _contains_secret(command, secrets):
                return {'status': 'error'}
            result = await (runner or _run)(command, directory, child, 120000)
            if result.exit != 0:
                return {'status': 'absent'} if _service_error(result.err, 'GetObject') == 'NoSuchKey' else {'status': 'error'}
            if _contains_secret(_etag(result.out), secrets):
                return {'status': 'error'}
            return {'status': 'present'}
    except Exception:
        return {'status': 'error'}


def _state(output):
    # Reuse the strict v4 envelope validator; inspect only validated envelope.
    params = _params(output)
    return json.loads(output), params


def _empty(state):
    return state['resources'] == [] and state['outputs'] == {}


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


def _documents(documents, provider):
    if not isinstance(documents, dict) or not documents:
        return False
    templates = json.loads(files('colors_compute').joinpath('templates.json').read_text()).get(provider)
    if not templates:
        return False
    namespaces = {name for stage in templates.values() for doc in stage.values() for name in doc.get('provider', {})}
    provider_specs = {name: spec for stage in templates.values() for doc in stage.values() for name, spec in doc.get('terraform', {}).get('required_providers', {}).items()}
    provider_fields = {name: set(config) for stage in templates.values() for doc in stage.values() for name, config in doc.get('provider', {}).items()}
    resource_types = {kind: {name for stage in templates.values() for doc in stage.values() for name in doc.get(kind, {})} for kind in ('resource', 'data')}
    for filename, document in documents.items():
        if not isinstance(filename, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*\.tf\.json', filename) or filename == 'backend.tf.json' or not isinstance(document, dict):
            return False
        if set(document) - {'terraform', 'provider', 'resource', 'data', 'locals', 'output'}:
            return False
        terraform = document.get('terraform', {})
        providers = document.get('provider', {})
        if not isinstance(terraform, dict) or 'backend' in terraform or not isinstance(providers, dict) or set(providers) - namespaces:
            return False
        required = terraform.get('required_providers', {})
        if not isinstance(required, dict) or any(name not in provider_specs or spec != provider_specs[name] for name, spec in required.items()):
            return False
        if any(not isinstance(config, dict) or set(config) - provider_fields[name] for name, config in providers.items()):
            return False
        for kind in ('resource', 'data'):
            if not isinstance(document.get(kind, {}), dict) or set(document.get(kind, {})) - resource_types[kind]:
                return False
        text = json.dumps(document, allow_nan=False)
        if '-----BEGIN ' in text or '"private_key"' in text or '"provisioner"' in text:
            return False
    return True


def _virgin_state(output):
    try:
        state = json.loads(output)
        return (isinstance(state, dict) and set(state) <= {'version', 'terraform_version', 'serial', 'lineage', 'outputs', 'resources', 'check_results'}
                and type(state.get('version')) is int and state['version'] == 4
                and type(state.get('serial')) is int and state['serial'] == 0 and state.get('lineage') == ''
                and state.get('outputs') == {} and state.get('resources') == [] and state.get('check_results') is None)
    except (ValueError, TypeError):
        return False


async def _converge_state(opts, state_key, documents, operation, presence, environment=None, runner=None, sleeper=None, decoder=None):
    """Execute one approved plan; caller must hold committed coordinator intent."""
    try:
        if not _state_key(opts, state_key) or operation not in ('create', 'delete', 'check') or presence not in ({'status': 'present'}, {'status': 'absent'}):
            return {'status': 'error'}
        if operation == 'check' and presence != {'status': 'present'}:
            return {'status': 'error'}
        protect = opts.get('compute-prevent-destroy', True)
        if type(protect) is not bool or operation == 'delete' and protect:
            return {'status': 'error'}
        provider = opts.get('provider-compute')
        if not isinstance(provider, str) or provider not in registry()['compute'] or not _documents(documents, provider):
            return {'status': 'error'}
        policy = json.loads(files('colors_compute').joinpath('execution-policy.json').read_text()).get(provider, {}).get('destroy_retry')
        retry = policy if operation == 'delete' and policy and any(policy['resource_type'] in doc.get('resource', {}) for doc in documents.values()) else None
        plan = backend_plan(opts, state_key)
        if operation == 'delete' and presence == {'status': 'absent'}:
            return {'status': 'destroyed'}
        source = dict(os.environ if environment is None else environment)
        credentials, secrets = {}, []
        for variable, option in plan['credential_bindings'].items():
            value = source.get(variable)
            if not isinstance(value, str) or _missing(value):
                return {'status': 'error'}
            credentials[option] = value
            secrets.append(value)
        child = {key: value for key, value in source.items() if not key.startswith(('TF_', 'TOFU_', 'COLORS_PAR_'))}
        for key, variable in registry()['compute'][provider]['tofu-env'].items():
            value = source.get('COLORS_PAR_' + key.upper().replace('-', '_'))
            if not isinstance(value, str) or _missing(value):
                return {'status': 'error'}
            child[variable] = value
            secrets.append(value)
        if _contains_secret(documents, secrets):
            return {'status': 'error'}
        with tempfile.TemporaryDirectory(prefix='colors-compute-execution-') as directory:
            path = Path(directory)
            os.chmod(path, 0o700)
            for filename, document in documents.items():
                _write(path / filename, json.dumps(document, allow_nan=False).encode())
            _write(path / 'backend.tf.json', json.dumps(plan['config']).encode())
            credential_file = path / 'credentials.tfbackend.json'
            _write(credential_file, json.dumps(credentials).encode())
            plan_file = path / 'approved.tfplan'
            _write(plan_file, b'')
            child.update(TF_IN_AUTOMATION='1', TF_INPUT='0', TF_WORKSPACE='default', TF_DATA_DIR=str(path / '.terraform'))
            execute = runner or _run
            async def run(arguments, timeout=120000):
                command = ['tofu', *arguments]
                if _contains_secret(command, secrets):
                    raise ValueError('invalid command')
                result = await execute(command, directory, child, timeout)
                if result.exit != 0:
                    error = ValueError('execution failed')
                    error.retryable = bool(retry and arguments[0] == 'apply' and isinstance(result.err, str) and len(result.err) <= 1048576 and retry['error_text'] in result.err and not _contains_secret(result.err, secrets))
                    raise error
                return result.out
            await run(['init', '-input=false', '-no-color', '-reconfigure', f'-backend-config={credential_file}'])
            before = await run(['state', 'pull'])
            if presence == {'status': 'absent'} and _virgin_state(before):
                before = ''
            if before.strip():
                state, params = _state(before)
                if _empty(state):
                    if operation == 'delete':
                        return {'status': 'destroyed'}
                elif params.get('provider') != provider:
                    return {'status': 'error'}
            elif presence != {'status': 'absent'}:
                return {'status': 'error'}
            if operation == 'check':
                if not before.strip() or _empty(state):
                    return {'status': 'error'}
                await run(['plan', '-input=false', '-no-color', '-detailed-exitcode'], 1800000)
                return {'status': 'clean'}
            for attempt in range(retry['attempts'] if retry else 1):
                arguments = ['plan', '-input=false', '-no-color', f'-out={plan_file}']
                if operation == 'delete':
                    arguments.append('-destroy')
                await run(arguments, 1800000)
                if not _valid_plan(await run(['show', '-json', str(plan_file)]), operation):
                    return {'status': 'error'}
                try:
                    await run(['apply', '-input=false', '-no-color', str(plan_file)], 1800000)
                    break
                except ValueError as error:
                    if not getattr(error, 'retryable', False) or attempt + 1 >= retry['attempts']:
                        raise
                    observed, observed_params = _state(await run(['state', 'pull']))
                    if _empty(observed):
                        return {'status': 'destroyed'}
                    if observed_params.get('provider') != provider:
                        return {'status': 'error'}
                    await (sleeper or asyncio.sleep)(retry['delay_ms'] / 1000)
            after = await run(['state', 'pull'])
            if operation == 'delete' and not after.strip():
                return {'status': 'destroyed'}
            state, params = _state(after)
            if operation == 'delete':
                return {'status': 'destroyed'} if _empty(state) else {'status': 'error'}
            outputs = decoder(after) if decoder else _outputs(after)
            if params.get('provider') != provider or _contains_secret(outputs, secrets):
                return {'status': 'error'}
            return {'status': 'ready', 'params': params, 'outputs': outputs}
    except Exception:
        return {'status': 'error'}


async def check_state(opts, state_key, documents, environment=None, runner=None):
    """Caller holds deployment journal; zero-change plan only, never apply."""
    return await converge_state(opts, state_key, documents, 'check', {'status': 'present'}, environment, runner)


async def converge_state(opts, state_key, documents, operation, presence, environment=None, runner=None, sleeper=None):
    return await _converge_state(opts, state_key, documents, operation, presence, environment, runner, sleeper)
