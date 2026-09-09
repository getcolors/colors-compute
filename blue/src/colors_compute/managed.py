"""Managed control-plane planning and journal-guarded native execution."""
import asyncio
from importlib.resources import files
import inspect
import json
import os
import re

from ._copy import deepcopy
from .backend import _read_state
from .contract import _safe, _missing, compute_credential_errors
from .coordinator import Coordinator
from .execution import _converge_state, state_presence
from .journal import journal_get, _identity, _settings
from .managed_access import AccessDecoder, managed_kubeconfig_path, public_params
from .managed_journal import managed_coordination, managed_document_valid
from .rendering import backend_plan, provider_plan


def _recipes():
    return json.loads(files('colors_compute').joinpath('managed-providers.json').read_text())


def managed_name(opts):
    provider = opts.get('provider-compute')
    if provider not in _recipes():
        raise ValueError('managed Kubernetes provider unavailable')
    name = opts.get(provider + '-name')
    if _missing(name):
        name = opts.get('profile')
    if not _safe(name):
        raise ValueError('invalid managed Kubernetes name')
    return name


def _resolve(opts, request):
    if not isinstance(request, dict) or set(request) - {'legacy_state_keys'} or not _safe(opts.get('profile')):
        raise ValueError('invalid managed Kubernetes request')
    recipes = _recipes()
    provider = opts.get('provider-compute')
    if provider not in recipes:
        raise ValueError('managed Kubernetes provider unavailable')
    recipe = recipes[provider]
    name = opts.get(provider + '-name')
    if _missing(name):
        name = opts['profile']
    if not _safe(name):
        raise ValueError('invalid managed Kubernetes name')
    values = {field: opts.get(option) for field, option in recipe['options'].items()}
    if any(not isinstance(values.get(field), str) or _missing(values[field]) or '${' in values[field] or '%{' in values[field] for field in ('region', 'version', 'size')):
        raise ValueError('missing managed Kubernetes settings')
    if not re.fullmatch(recipe['version_pattern'], values['version']):
        raise ValueError('invalid managed Kubernetes version')
    count = values.get('count')
    if type(count) not in (int, float) or not 1 <= count <= 1000 or count != int(count):
        raise ValueError('invalid managed Kubernetes node count')
    protect = opts.get('compute-prevent-destroy', True)
    if type(protect) is not bool:
        raise ValueError('invalid compute protection')
    legacy = request.get('legacy_state_keys', [])
    if not isinstance(legacy, list) or len(legacy) != len(set(legacy)) or any(not isinstance(key, str) or not re.fullmatch(re.escape(opts['profile']) + r'/[A-Za-z0-9][A-Za-z0-9_-]{0,62}\.tfstate', key) for key in legacy):
        raise ValueError('invalid legacy compute state keys')
    managed_kubeconfig_path(opts)
    key = opts['profile'] + '/compute/managed-kubernetes.tfstate'
    backend_plan(opts, key)
    return provider, {**values, 'count': int(count), 'name': name, 'prevent_destroy': protect}, key


def managed_errors(opts, request=None):
    try:
        _resolve(opts, {} if request is None else request)
        return []
    except Exception as error:
        return [str(error)] if type(error) is ValueError else ['invalid managed Kubernetes request']


def plan_managed_kubernetes(opts, request=None):
    opts, request = deepcopy(opts), deepcopy({} if request is None else request)
    provider, inputs, key = _resolve(opts, request)
    params = {'provider': provider, 'kind': 'managed-kubernetes', 'name': inputs['name'],
              'cluster_id': 'planned-cluster', 'endpoint': 'https://192.0.2.10'}
    params.update(_recipes()[provider].get('planning_params', {}))
    return {'status': 'planned', 'params': params, 'state_key': key,
            'documents': {**provider_plan(provider, 'managed-kubernetes', inputs), 'backend.tf.json': backend_plan(opts, key)['config']}}


def managed_application_settings(opts, params=None):
    """Return provider details needed by application Kubernetes manifests."""
    provider, _, _ = _resolve(opts, {})
    params = plan_managed_kubernetes(opts)['params'] if params is None else public_params(params, provider)
    traits = _recipes()[provider]['traits']
    result = {'load_balancer_annotations': {
        key: value.replace('{{name}}', params['name'])
        for key, value in traits['load_balancer_annotations'].items()},
        'storage_class': traits['storage_class']}
    pod_cidr = params.get('pod_cidr') if traits['pod_cidr_source'] == 'observed' else opts.get(traits['pod_cidr_option'])
    if pod_cidr is not None:
        public_params({**params, 'pod_cidr': pod_cidr}, provider)
        result['pod_cidr'] = pod_cidr
    return result


def managed_application_artifacts(opts, required=None):
    """Return library-owned scripts for the selected managed provider."""
    provider, _, _ = _resolve(opts, {})
    artifacts = json.loads(files('colors_compute').joinpath('managed-artifacts.json').read_text())[provider]
    required = [] if required is None else required
    if not isinstance(required, list) or any(not isinstance(name, str) or name not in artifacts for name in required):
        raise ValueError('managed provider lacks required application artifact')
    return artifacts


async def _read_managed_state(opts, key, environment=None, runner=None, write=False):
    decoder = AccessDecoder(opts, environment)
    result = await _read_state(opts, key, environment, runner, True, decoder)
    if result.get('status') != 'present':
        return {'status': 'error'}
    if result.get('state_empty'):
        return result
    result = {'status': 'present', 'params': public_params(result['params'], opts['provider-compute'])}
    if write:
        result['kubeconfig_path'] = decoder.write()
    return result


async def read_managed_kubernetes(opts, request=None, environment=None, dependencies=None):
    deps = dependencies or {}
    env = dict(os.environ if environment is None else environment)
    async def call(name, default, *args, **kwargs):
        value = deps.get(name, default)(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value
    coordinator, acquired = None, False
    result = {'status': 'error'}
    try:
        _, _, key = _resolve(opts, {} if request is None else request)
        observed = await call('journal_get', journal_get, opts, env)
        if observed == {'status': 'absent'}:
            return observed
        document = observed.get('document')
        if observed.get('status') != 'present' or not managed_document_valid(document) or document['identity'] != _identity(opts, _settings(opts)) or document['lock']['state'] != 'idle':
            return {'status': 'error'}
        if document['status'] == 'retired':
            return {'status': 'destroyed'}
        if document['shared']['phase'] not in ('ready', 'failed'):
            return {'status': 'error'}
        async def existing():
            value = await call('journal_get', journal_get, opts, env)
            if value.get('status') != 'present':
                raise ValueError('managed journal unavailable')
            return value
        coordinator = deps.get('coordinator', Coordinator)(opts, env, read=existing, event_prefix='managed/', reducer=managed_coordination)
        await coordinator.acquire()
        acquired = True
        document = (await coordinator.snapshot())['document']
        if document['status'] == 'retired':
            result = {'status': 'destroyed'}
        elif document['shared']['phase'] not in ('ready', 'failed'):
            result = {'status': 'error'}
        else:
            result = await call('read_managed_state', _read_managed_state, opts, key, env, write=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        result = {'status': 'error'}
    finally:
        if acquired:
            try:
                await coordinator.release()
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {'status': 'error'}
    return result


async def managed_version_preflight(opts, environment):
    """Provider-owned version discovery without argv credentials or redirects."""
    from urllib.request import Request, build_opener, HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    recipe = _recipes()[opts['provider-compute']]
    descriptor = recipe['versions']
    def read():
        token = environment.get(descriptor['credential'])
        if not isinstance(token, str) or not token.strip():
            return False
        request = Request(descriptor['url'], headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            raw = response.read(2097153)
        if len(raw) > 2097152:
            return False
        value = json.loads(raw.decode('utf-8'))
        for field in descriptor['path']:
            value = value[field]
        if not isinstance(value, list) or not value:
            return False
        versions = [item[descriptor['value']] for item in value] if descriptor['value'] else value
        return all(isinstance(item, str) for item in versions) and opts[recipe['options']['version']] in versions
    try:
        return await asyncio.to_thread(read)
    except Exception:
        return False


async def managed_kubernetes(opts, request=None, environment=None, dependencies=None):
    opts, request = deepcopy(opts), deepcopy({} if request is None else request)
    deps = dependencies or {}
    env = dict(os.environ if environment is None else environment)
    coordinator, acquired = None, False
    result = {'status': 'error'}
    async def call(name, default, *args, **kwargs):
        value = deps.get(name, default)(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value
    def require(ok):
        if not ok:
            raise ValueError('managed Kubernetes lifecycle refused')
    try:
        errors = managed_errors(opts, request)
        if errors:
            return {'status': 'error', 'errors': errors}
        plan = plan_managed_kubernetes(opts, request)
        operation = opts.get('blue/event', 'create')
        require(operation in ('create', 'delete') and opts.get('blue/dry-run') is not True)
        require(operation != 'delete' or opts.get('compute-prevent-destroy') is False)
        coordinator = deps.get('coordinator', Coordinator)(opts, env, event_prefix='managed/', reducer=managed_coordination)
        await coordinator.acquire()
        acquired = True
        for legacy in request.get('legacy_state_keys', []):
            require(await call('state_presence', state_presence, opts, legacy, env, legacy=True) == {'status': 'absent'})
        doc = (await coordinator.snapshot())['document']
        presence = await call('state_presence', state_presence, opts, plan['state_key'], env)
        require(presence in ({'status': 'present'}, {'status': 'absent'}))
        phase = doc['shared']['phase']
        if phase in ('declared', 'destroyed'):
            if presence == {'status': 'present'}:
                empty = await call('read_empty', _read_state, opts, plan['state_key'], env, include_outputs=True)
                require(empty.get('status') == 'present' and empty.get('state_empty') is True)
        else:
            require(presence == {'status': 'present'})
            observed = await call('read_managed_state', _read_managed_state, opts, plan['state_key'], env)
            require(observed.get('status') == 'present' and observed.get('params', {}).get('provider') == opts['provider-compute'])
        if doc['status'] == 'retired':
            if operation == 'delete':
                result = {'status': 'destroyed'}
            else:
                await coordinator.transition('recreate')
        if result.get('status') != 'destroyed':
            if phase == 'failed' and operation == 'create':
                require(doc['shared']['operation'] == 'create')
                await coordinator.transition('shared-retry', evidence='readable-state')
            errors = compute_credential_errors(opts, env)
            if errors and not (operation == 'delete' and presence == {'status': 'absent'}):
                result = {'status': 'error', 'errors': errors}
            else:
                if operation == 'create':
                    require(await call('version_preflight', managed_version_preflight, opts, env) is True)
                attempt = await (coordinator.shared_start() if operation == 'create' else coordinator.shared_destroy())
                decoder = AccessDecoder(opts, env)
                outcome = await call('converge_state', _converge_state, opts, plan['state_key'], {name: value for name, value in plan['documents'].items() if name != 'backend.tf.json'}, operation, presence, env, decoder=decoder)
                if outcome.get('status') == ('ready' if operation == 'create' else 'destroyed'):
                    await coordinator.shared_complete(attempt)
                    result = {'status': outcome['status']}
                    if operation == 'create':
                        result.update(params=public_params(outcome['params'], opts['provider-compute']), kubeconfig_path=decoder.write())
                else:
                    await coordinator.shared_fail(attempt)
    except asyncio.CancelledError:
        raise
    except Exception:
        result = {'status': 'error'}
    finally:
        if acquired:
            try:
                await coordinator.release()
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {'status': 'error'}
    return result
