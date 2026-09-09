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
    for node in assembly['nodes']:
        provider_request(planning_opts, 'node', node, recipe['planning_shared'])
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
    network = ipaddress.ip_network(cidr, strict=True)
    shared['params']['network_cidr'] = str(network)
    for ordinal, node in enumerate(assembly['nodes']):
        plan = provider_request(opts, 'node', node, shared)
        documents['nodes'][node['node_id']] = plan['documents']
        offset = ordinal + 10
        if offset >= network.num_addresses - 1:
            raise ValueError('build exceeds private network address capacity')
        params = {'node_id': node['node_id'], 'provider': provider, 'name': node['name'],
                  'ip': '192.0.2.' + str(offset), 'vpc_ip': str(network.network_address + offset),
                  'user': entry['user'], 'sudoer': entry['sudoer']}
        if selected['mode'] == 'managed':
            params['ssh_identity_file'] = '$HOME/.ssh/' + opts['profile']
        elif selected.get('private_key_path'):
            params['ssh_identity_file'] = selected['private_key_path']
        results.append(params)
    return {'status': 'planned', 'documents': documents, 'shared': shared,
            'state_keys': state_keys(opts['profile'], [node['node_id'] for node in declarations]),
            'cluster': collect(declarations, results, declarations[0]['node_id']),
            'key': {'mode': selected['mode'], **({'private_key_path': '$HOME/.ssh/' + opts['profile']} if selected['mode'] == 'managed' else
                    {'private_key_path': selected['private_key_path']} if selected.get('private_key_path') else {})}}
