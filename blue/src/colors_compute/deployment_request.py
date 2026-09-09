"""Translate application requirements into common shared and node requests."""
from ._copy import deepcopy
from importlib.resources import files
import json
import re

from .contract import _missing, _safe, expand


def source_cidrs(opts, suffix, application_key=None):
    """Resolve an application source list with legacy provider-scoped fallback."""
    if not isinstance(suffix, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', suffix):
        raise ValueError('invalid compute source setting')
    if application_key is not None and application_key in opts:
        value = opts[application_key]
    else:
        provider = opts.get('provider-compute')
        value = opts.get(str(provider) + '-' + suffix)
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in re.split(r'[\s,]+', value.strip()) if part]
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return deepcopy(value)
    raise ValueError('invalid compute source list')


def deployment_requests(opts, topology, requirements, key):
    recipes = json.loads(files('colors_compute').joinpath('provider-recipes.json').read_text())
    provider = opts.get('provider-compute')
    if not isinstance(provider, str) or provider not in recipes:
        raise ValueError('compute provider recipe unavailable')
    if not isinstance(requirements, dict) or set(requirements) - {'security', 'network', 'single_host', 'legacy_state_keys', 'private', 'endpoint'} or 'security' not in requirements:
        raise ValueError('invalid deployment requirements')
    single = requirements.get('single_host', False)
    if type(single) is not bool:
        raise ValueError('invalid single-host requirement')
    nodes = expand(topology)
    if len(nodes) > 1000 or single and (len(nodes) != 1 or nodes[0]['role'] is not None):
        raise ValueError('invalid deployment topology')
    profile = opts.get('profile')
    if not _safe(profile):
        raise ValueError(':profile must be a safe identifier')
    name = opts.get(provider + '-name')
    if _missing(name):
        name = profile
    if not _safe(name):
        raise ValueError('invalid compute name')
    network = deepcopy(requirements.get('network', {}))
    if not isinstance(network, dict):
        raise ValueError('invalid compute network request')
    network.setdefault('mode', recipes[provider]['network_mode'])
    # The key runtime returns local path references in addition to public data.
    # Only fields accepted by the renderer cross this boundary.
    public_key = {field: deepcopy(key[field]) for field in ('mode', 'public_key', 'ids', 'reference') if field in key}
    base = {'key': public_key, 'network': network, 'security': deepcopy(requirements['security'])}
    if 'endpoint' in requirements:
        base['endpoint'] = deepcopy(requirements['endpoint'])
    requests = []
    for node in nodes:
        node_name = name if single else name + '-' + node['node_id']
        if not _safe(node_name):
            raise ValueError('invalid derived compute name')
        requests.append({**deepcopy(base), 'node_id': node['node_id'], 'name': node_name})
    return {'shared': {**deepcopy(base), 'node_id': 'shared', 'name': name}, 'nodes': requests}
