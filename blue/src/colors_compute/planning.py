"""Credential-free artifacts and inventory for build and dry-run."""
from ._copy import deepcopy
from importlib.resources import files
import ipaddress
import json

from .contract import collect, expand, registry, state_keys
from .deployment_request import deployment_requests
from .key_request import key_request
from .provider_request import provider_request
from .ssh import _mode, PLACEHOLDER


def validate_deployment(opts, topology, requirements):
    """Validate all requested capabilities and bindings before key generation."""
    planning_opts = {**deepcopy(opts), 'blue/event': 'build'}
    selected = _mode(planning_opts)
    if selected['mode'] == 'managed':
        selected['public_key'] = PLACEHOLDER
    key = key_request(planning_opts, selected, {})
    assembly = deployment_requests(planning_opts, topology, requirements, key)
    provider = planning_opts['provider-compute']
    recipe = json.loads(files('colors_compute').joinpath('provider-recipes.json').read_text())[provider]
    provider_request(planning_opts, 'shared', assembly['shared'])
    shared = deepcopy(recipe['planning_shared'])
    if 'id' in assembly['shared']['network']:
        shared['params']['vpc_id'] = assembly['shared']['network']['id']
    if 'roles' in requirements:
        shared['params']['role_firewall_ids'] = {role: 'build-firewall-' + role for role in requirements['roles']}
        if recipe.get('role_tag_param'):
            shared['params']['role_tags'] = {role: 'colors-compute-' + opts['profile'] + '-' + role for role in requirements['roles']}
    for node in assembly['nodes']:
        provider_request(planning_opts, 'node', node, shared)
    return True


def plan_deployment(opts, topology, requirements):
    """Return deterministic documents and documentation addresses without I/O."""
    opts = {**deepcopy(opts), 'blue/event': 'build'}
    selected = _mode(opts)
    if selected['mode'] == 'managed':
        selected['public_key'] = PLACEHOLDER
    key = key_request(opts, selected, {})
    assembly = deployment_requests(opts, topology, requirements, key)
    provider = opts['provider-compute']
    recipe = json.loads(files('colors_compute').joinpath('provider-recipes.json').read_text())[provider]
    shared = deepcopy(recipe['planning_shared'])
    if 'id' in assembly['shared']['network']:
        shared['params']['vpc_id'] = assembly['shared']['network']['id']
    if 'roles' in requirements:
        shared['params']['role_firewall_ids'] = {role: 'build-firewall-' + role for role in requirements['roles']}
        if recipe.get('role_tag_param'):
            shared['params']['role_tags'] = {role: 'colors-compute-' + opts['profile'] + '-' + role for role in requirements['roles']}
    shared_plan = provider_request(opts, 'shared', assembly['shared'])
    declarations = expand(topology)
    if len(declarations) > 245:
        raise ValueError('build exceeds documentation address capacity')
    entry = registry()['compute'][provider]
    documents = {'shared': shared_plan['documents'], 'nodes': {}}
    results = []
    cidr = assembly['shared']['network'].get('subnet_cidr') or assembly['shared']['network'].get('cidr')
    if cidr is None:
        cidr = next((opts[name] for name in recipe['subnet_cidr_options'] + recipe['network_cidr_options'] if opts.get(name)), '10.0.0.0/24')
    network = None if assembly['shared']['network']['mode'] == 'none' else ipaddress.ip_network(cidr, strict=True)
    if network is None:
        for name in ('vpc_id', 'vpc_ip_range', 'network_cidr', 'subnet_id', 'subnet_cidr'):
            shared['params'].pop(name, None)
    else:
        shared['params']['network_cidr'] = str(network)
    if 'endpoint' in requirements:
        shared['params']['endpoint_ip'] = '198.51.100.10'
    for ordinal, node in enumerate(assembly['nodes']):
        plan = provider_request(opts, 'node', node, shared)
        documents['nodes'][node['node_id']] = plan['documents']
        offset = ordinal + 10
        if network is not None and offset >= network.num_addresses - 1:
            raise ValueError('build exceeds private network address capacity')
        params = {'provider_id': str(int(recipe['planning_provider_id']) + ordinal) if recipe['planning_provider_id'].isdigit() else recipe['planning_provider_id'] + '-' + node['node_id'], 'node_id': node['node_id'], 'provider': provider, 'name': node['name'],
                  'ip': '192.0.2.' + str(offset), 'vpc_ip': str(network.network_address + offset) if network is not None else None,
                  'user': entry['user'], 'sudoer': entry['sudoer']}
        if selected['mode'] == 'managed':
            params['ssh_identity_file'] = '$HOME/.ssh/' + opts['profile']
        elif selected.get('private_key_path'):
            params['ssh_identity_file'] = selected['private_key_path']
        results.append(params)
    if 'roles' in requirements:
        roles_by_id = {node['node_id']: node['role'] for node in declarations}
        assembly['shared']['peers'] = {node['node_id']: {'role': roles_by_id[node['node_id']], 'vpc_ip': node['vpc_ip']} for node in results}
        documents['shared'] = provider_request(opts, 'shared', assembly['shared'])['documents']
    return {'status': 'planned', 'documents': documents, 'shared': shared,
            'state_keys': state_keys(opts['profile'], [node['node_id'] for node in declarations]),
            'cluster': collect(declarations, results, assembly['entry_node_id']),
            'key': {'mode': selected['mode'], **({'private_key_path': '$HOME/.ssh/' + opts['profile']} if selected['mode'] == 'managed' else
                    {'private_key_path': selected['private_key_path']} if selected.get('private_key_path') else {})}}
