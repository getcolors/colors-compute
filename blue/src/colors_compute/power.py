"""Serialized power transitions for an owned singleton; no OpenTofu apply."""
import asyncio
import inspect
import ipaddress
import json
import os
import re
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from .registration import _NoRedirect
from ._copy import deepcopy
from .backend import _run, read_state
from .contract import collect, _missing, registry, state_keys
from .coordinator import Coordinator
from .journal import journal_get, journal_put
from .lifecycle import lifecycle_document_valid
from .ssh import _mode

DESCRIPTORS = json.loads(files('colors_compute').joinpath('power-providers.json').read_text())

def _require(value):
    if not value:
        raise ValueError('compute power refused')

def _json(value):
    _require(isinstance(value, str) and len(value.encode()) <= 2097152)
    return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))

def _safe_output(value, env):
    serialized = json.dumps(value, ensure_ascii=False)
    names = {'COLORS_PAR_' + key.upper().replace('-', '_') for section in ('compute', 'backend') for entry in registry()[section].values() for key in entry.get('secrets', [])}
    _require(not any(secret in serialized or json.dumps(secret, ensure_ascii=False)[1:-1] in serialized
                     for key, secret in env.items() if key in names and isinstance(secret, str) and secret))

def _get_http(method, url, headers):
    request = Request(url, data=b'' if method == 'POST' else None, headers=headers, method=method)
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=30) as response:
        _require(200 <= response.status < 300)
        deadline = time.monotonic() + 30
        chunks, size = [], 0
        while True:
            if response.fp is None:
                break
            remaining = deadline - time.monotonic()
            _require(remaining > 0)
            response.fp.raw._sock.settimeout(remaining)
            chunk = response.read1(min(65536, 2097153 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            _require(size <= 2097152)
        return b''.join(chunks).decode('utf-8')

async def _provider_power(opts, action, provider_id, environment, dependencies=None):
    deps, env = dependencies or {}, dict(environment)
    descriptor = DESCRIPTORS.get(opts.get('provider-compute'))
    _require(descriptor and action in descriptor['actions'])
    _require(isinstance(provider_id, str) and re.fullmatch(descriptor['id_pattern'], provider_id))
    wait = opts.get('power-wait-seconds', 300)
    _require(type(wait) in (int, float) and int(wait) == wait and 1 <= wait <= 1800)
    target = descriptor['actions'][action]
    async def call(name, default, *args):
        result = deps.get(name, default)(*args)
        return await result if inspect.isawaitable(result) else result
    if descriptor['transport'] == 'oci':
        profile = opts.get('oci-config-file-profile')
        _require(isinstance(profile, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', profile))
        child_env = {k: v for k, v in env.items() if not k.startswith(('TF_', 'TOFU_', 'OCI_CLI_')) or k == 'OCI_CLI_AUTH'}
        prefix = ['oci', '--config-file', str(Path(env.get('HOME') or str(Path.home())) / '.oci/config'), '--profile', profile,
                  '--cli-rc-file', '/dev/null', '--no-retry', '--output', 'json']
        async def command(arguments, timeout):
            _safe_output(prefix + arguments, env)
            with tempfile.TemporaryDirectory(prefix='colors-power-') as directory:
                result = await call('runner', _run, prefix + arguments, directory, child_env, timeout)
            code = result.get('exit') if isinstance(result, dict) else result.exit
            out = result.get('out') if isinstance(result, dict) else result.out
            _require(type(code) in (int, float) and code == 0)
            return _json(out)
        arguments = ['compute', 'instance', 'get', '--instance-id', provider_id]
        instance = (await command(arguments, 30000)).get('data', {})
        _require(instance.get('id') == provider_id and isinstance(instance.get('lifecycle-state'), str))
        if instance['lifecycle-state'] != target['state']:
            await command(['compute', 'instance', 'action', '--instance-id', provider_id, '--action', target['action'],
                           '--wait-for-state', target['state'], '--max-wait-seconds', str(int(wait))], int(wait * 1000 + 30000))
            instance = (await command(arguments, 30000)).get('data', {})
        _require(instance.get('id') == provider_id and instance.get('lifecycle-state') == target['state'])
        result = {'status': 'ready'}
        if action == 'start':
            vnics = (await command(['compute', 'instance', 'list-vnics', '--instance-id', provider_id], 30000)).get('data')
            _require(isinstance(vnics, list))
            addresses = [v.get('public-ip') for v in vnics if isinstance(v, dict) and v.get('public-ip')]
            _require(len(addresses) == 1)
            result['ip'] = str(ipaddress.IPv4Address(addresses[0]))
    else:
        token = env.get(descriptor['credential'])
        _require(isinstance(token, str) and not _missing(token) and '\n' not in token and '\r' not in token)
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        url = descriptor['origin'] + '/instances/' + provider_id
        async def http(method, endpoint):
            value = await call('http', lambda m, u, h: asyncio.to_thread(_get_http, m, u, h), method, endpoint, headers.copy())
            return _json(value) if method == 'GET' else None
        async def current():
            instance = (await http('GET', url)).get('instance', {})
            _require(instance.get('id') == provider_id and isinstance(instance.get('power_status'), str))
            return instance
        instance = await current()
        if instance['power_status'] != target['state']:
            await http('POST', url + '/' + target['action'])
            deadline = time.monotonic() + wait
            for _ in range(int(wait / 5) + 2):
                instance = await current()
                if instance['power_status'] == target['state']:
                    break
                _require(time.monotonic() < deadline)
                await call('sleep', asyncio.sleep, min(5, max(0, deadline - time.monotonic())))
        _require(instance['power_status'] == target['state'])
        result = {'status': 'ready'}
        if action == 'start':
            result['ip'] = str(ipaddress.IPv4Address(instance.get('main_ip')))
    _safe_output(result, env)
    return result

async def provider_power(opts, action, provider_id, environment, dependencies=None):
    try:
        return await _provider_power(opts, action, provider_id, environment, dependencies)
    except Exception:
        raise ValueError('compute power refused') from None

async def power_deployment(opts, action, environment=None, dependencies=None):
    opts = deepcopy(opts)
    env, deps = dict(os.environ if environment is None else environment), dependencies or {}
    owner, acquired, dispatched = None, False, False
    async def call(name, default, *args):
        value = deps.get(name, default)(*args)
        return await value if inspect.isawaitable(value) else value
    async def existing():
        observed = await call('journal_get', journal_get, opts, env)
        _require(observed.get('status') == 'present')
        return observed
    def valid(doc):
        _require(lifecycle_document_valid(doc) and doc['status'] == 'active' and doc['topology_declared']
                 and doc['shared']['phase'] == 'ready' and doc['key']['phase'] == 'prepared')
        active = [(id, node) for id, node in doc['nodes'].items() if node['phase'] != 'destroyed']
        _require(len(active) == 1 and active[0][1]['desired'] and active[0][1]['phase'] == 'ready')
        _require(all(not node['desired'] for node in doc['nodes'].values() if node['phase'] == 'destroyed'))
        return active[0]
    try:
        descriptor = DESCRIPTORS.get(opts.get('provider-compute'))
        _require(descriptor and action in descriptor['actions'])
        if opts.get('blue/event') == 'build' or opts.get('blue/dry-run') is True:
            return {'status': 'planned', 'action': action}
        observed = await existing()
        valid(observed['document'])
        _require(observed['document']['lock']['state'] == 'idle')
        owner = Coordinator(opts, environment=env, read=existing, write=lambda intent: call('journal_put', journal_put, opts, intent, env), event_prefix='lifecycle/')
        await owner.acquire()
        acquired = True
        doc = (await owner.snapshot())['document']
        node_id, node = valid(doc)
        selected = _mode(opts)
        _require(selected['mode'] == doc['key']['mode'])
        shared = await call('read_state', read_state, opts, state_keys(opts['profile'], [])['shared'], env)
        _require(shared.get('status') == 'present' and shared.get('params', {}).get('provider') == opts['provider-compute'])
        state = await call('read_state', read_state, opts, node['state_key'], env)
        _require(state.get('status') == 'present')
        declarations = [{'node_id': node_id, 'role': node['role'], 'index': node['index'], 'provider': opts['provider-compute']}]
        cluster = collect(declarations, [state.get('params', {})], node_id)
        provider_id = cluster['nodes'][0].get('provider_id')
        _require(isinstance(provider_id, str) and re.fullmatch(descriptor['id_pattern'], provider_id))
        _safe_output(provider_id, env)
        dispatched = True
        powered = await call('provider_power', provider_power, opts, action, provider_id, env)
        _require(powered.get('status') == 'ready')
        if action == 'start':
            cluster['nodes'][0]['ip'] = str(ipaddress.IPv4Address(powered.get('ip')))
        key = {'mode': selected['mode']}
        identity = str(Path(env.get('HOME') or str(Path.home())) / '.ssh' / opts['profile']) if selected['mode'] == 'managed' else selected.get('private_key_path')
        if identity:
            key['private_key_path'] = identity
            cluster['nodes'][0]['ssh_identity_file'] = identity
        result = {'status': 'ready', 'action': action, 'cluster': cluster, 'key': key}
        _safe_output(result, env)
        await owner.release()
        acquired = False
        return result
    except asyncio.CancelledError:
        raise
    except Exception:
        if acquired and not dispatched:
            try:
                await owner.release()
            except Exception:
                pass
        return {'status': 'error'}
