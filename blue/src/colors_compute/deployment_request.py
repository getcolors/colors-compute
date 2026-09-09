"""Translate application requirements into common shared and node requests."""
from ._copy import deepcopy
from importlib.resources import files
import json
import re

from .contract import _missing, _safe, expand
from .controller import controller_artifact


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
    if not isinstance(requirements, dict) or set(requirements) - {'security', 'network', 'single_host', 'legacy_state_keys', 'private', 'endpoint', 'roles', 'entry_node_id', 'kubernetes_controller', 'backups', 'ipv6'} or 'security' not in requirements:
        raise ValueError('invalid deployment requirements')
    if 'kubernetes_controller' in requirements:
        if requirements['kubernetes_controller'] is not True:
            raise ValueError('invalid Kubernetes controller requirement')
        controller_artifact(opts, recipes[provider]['planning_shared'])
    single = requirements.get('single_host', False)
    if type(single) is not bool:
        raise ValueError('invalid single-host requirement')
    nodes = expand(topology)
    if len(nodes) > 1000 or single and (len(nodes) != 1 or nodes[0]['role'] is not None):
        raise ValueError('invalid deployment topology')
    role_names = {node['role'] for node in nodes if node['role'] is not None}
    roles = requirements.get('roles')
    if roles is not None:
        if not isinstance(roles, dict) or set(roles) != role_names or any(node['role'] is None for node in nodes):
            raise ValueError('invalid deployment role policies')
        for role, policy in roles.items():
            if not isinstance(policy, dict) or set(policy) != {'security'} or not isinstance(policy['security'], dict) or not isinstance(policy['security'].get('ingress'), list):
                raise ValueError('invalid deployment role policies')
            for rule in policy['security']['ingress']:
                if not isinstance(rule, dict):
                    raise ValueError('invalid deployment role policies')
                if 'peer_roles' in rule and (not isinstance(rule['peer_roles'], list) or not rule['peer_roles'] or
                    any(not isinstance(peer, str) or peer not in role_names for peer in rule['peer_roles']) or len(set(rule['peer_roles'])) != len(rule['peer_roles'])):
                    raise ValueError('invalid deployment peer roles')
    entry = requirements.get('entry_node_id', nodes[0]['node_id'])
    if not isinstance(entry, str) or entry not in {node['node_id'] for node in nodes}:
        raise ValueError('invalid deployment entry node')
    settings = opts.get('compute-role-settings', {})
    if not isinstance(settings, dict) or set(settings) - role_names:
        raise ValueError('invalid compute role settings')
    for value in settings.values():
        if not isinstance(value, dict) or set(value) - {'size', 'image'} or any(_missing(v) for v in value.values()):
            raise ValueError('invalid compute role settings')
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
    default_mode = 'none' if single and requirements.get('private', False) is False and 'none' in recipes[provider].get('network_modes', []) else recipes[provider]['network_mode']
    network.setdefault('mode', default_mode)
    if network['mode'] == 'none' and (single is not True or requirements.get('private', False) is not False):
        raise ValueError('network none requires public-only single host')
    # The key runtime returns local path references in addition to public data.
    # Only fields accepted by the renderer cross this boundary.
    public_key = {field: deepcopy(key[field]) for field in ('mode', 'public_key', 'ids', 'reference') if field in key}
    base = {'key': public_key, 'network': network, 'security': deepcopy(requirements['security'])}
    if 'endpoint' in requirements:
        base['endpoint'] = deepcopy(requirements['endpoint'])
    base.update({field: deepcopy(requirements[field]) for field in ('backups', 'ipv6') if field in requirements})
    requests = []
    for node in nodes:
        node_name = name if single else name + '-' + node['node_id']
        if not _safe(node_name):
            raise ValueError('invalid derived compute name')
        item = {**deepcopy(base), 'node_id': node['node_id'], 'name': node_name}
        if node['role'] is not None:
            item['role'] = node['role']
        if roles is not None:
            item['security'] = deepcopy(roles[node['role']]['security'])
            item['roles'] = deepcopy(roles)
        requests.append(item)
    shared = {**deepcopy(base), 'node_id': 'shared', 'name': name}
    if roles is not None:
        shared['roles'] = deepcopy(roles)
    return {'shared': shared, 'nodes': requests, 'entry_node_id': entry}
