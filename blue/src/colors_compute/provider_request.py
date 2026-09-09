"""Library-owned provider-neutral request normalization and declarative bindings."""
from ._copy import deepcopy
from importlib.resources import files
import hashlib
import ipaddress
import json
import re

from .contract import _safe, _missing, registry
from .rendering import provider_plan


def _fail(message):
    raise ValueError(message)


def _integer(value, low, high):
    return type(value) in (int, float) and low <= value <= high and value == int(value)


def _fields(value, required, optional=()):
    return isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional)


def _cidr(value):
    if not isinstance(value, str):
        _fail('invalid compute network CIDR')
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        _fail('invalid compute network CIDR')
    if network.version != 4 or str(network) != value:
        _fail('unsupported compute network address family')
    return network


def _binding(spec, context):
    if 'value' in spec:
        return deepcopy(spec['value'])
    if 'path' in spec:
        current = context
        for key in spec['path'].split('.'):
            current = current.get(key) if isinstance(current, dict) else None
        if _missing(current):
            if 'default' in spec:
                return deepcopy(spec['default'])
            _fail('missing compute input: ' + spec['path'])
        return deepcopy(current)
    if 'first' in spec:
        for candidate in spec['first']:
            try:
                return _binding(candidate, context)
            except ValueError as error:
                if not str(error).startswith('missing compute input: '):
                    raise
        _fail('missing compute input: ' + spec['first'][0]['path'])
    if 'list' in spec:
        return [_binding(item, context) for item in spec['list']]
    if 'object' in spec:
        return {key: _binding(item, context) for key, item in spec['object'].items()}
    _fail('invalid provider recipe')


def _rules(format_name, request, network, name):
    expanded = []
    for rule in sorted(request['security']['ingress'], key=lambda item: item['id']):
        for source in sorted(rule['sources']):
            private = source == 'private'
            cidr = network if private else source
            if private and format_name == 'digitalocean':
                cidr = None  # Template binds discovered data source, never a fixture CIDR.
            elif private and cidr is None:
                _fail('missing compute network CIDR for private ingress')
            expanded.append((rule['id'] + ':' + source, rule, cidr, private))
    result, public, private_rules, public_rules = {}, {}, {}, []
    for ordinal, (key, rule, cidr, private) in enumerate(expanded):
        protocol = rule['protocol']
        icmp = protocol == 'icmp'
        lo, hi = (None, None) if icmp else (int(rule['from_port']), int(rule['to_port']))
        ports = None if icmp else str(lo) if lo == hi else f'{lo}-{hi}'
        if format_name == 'vultr':
            net = _cidr(cidr)
            result[key] = {'protocol': protocol, 'port': ports, 'ip_type': 'v4', 'subnet': str(net.network_address), 'subnet_size': net.prefixlen}
        elif format_name == 'aws':
            result[key] = {'protocol': protocol, 'from_port': -1 if icmp else lo, 'to_port': -1 if icmp else hi, 'cidr': cidr}
        elif format_name == 'azure':
            if ordinal >= 3996:
                _fail('too many compute ingress rules')
            result[key.replace(':', '-').replace('/', '-').replace('.', '-')] = {'priority': 100 + ordinal, 'protocol': protocol.capitalize(), 'port': '*' if icmp else ports, 'sources': [cidr]}
        elif format_name == 'google':
            result[key] = {'name': name + '-' + hashlib.sha256(key.encode()).hexdigest()[:12], 'protocol': protocol, 'ports': [] if icmp else [ports], 'source_ranges': [cidr]}
        elif format_name == 'yandex':
            result[key] = {'protocol': protocol.upper(), 'from_port': lo, 'to_port': hi, 'cidr_blocks': [cidr]}
        elif format_name == 'digitalocean':
            value = {'protocol': protocol, **({} if icmp else {'port_range': ports})}
            if not private:
                value['source_addresses'] = [cidr]
            (private_rules if private else public)[key] = value
        elif format_name == 'hcloud':
            public_rules.append({'direction': 'in', 'protocol': protocol, **({} if icmp else {'port': ports}), 'source_ips': [cidr]})
        elif format_name == 'oci':
            result[key] = {'direction': 'INGRESS', 'protocol': '1' if icmp else '6' if protocol == 'tcp' else '17', 'cidr': cidr, 'from_port': lo, 'to_port': hi}
        else:
            _fail('unsupported compute firewall format')
    if format_name == 'oci':
        result['outbound-all'] = {'direction': 'EGRESS', 'protocol': 'all', 'cidr': '0.0.0.0/0', 'from_port': None, 'to_port': None}
    egress = {'all-ipv4': {'protocol': '-1', 'from_port': None, 'to_port': None, 'cidr': '0.0.0.0/0'}}
    if format_name == 'yandex':
        egress = {'all-ipv4': {'protocol': 'ANY', 'from_port': 0, 'to_port': 65535, 'cidr_blocks': ['0.0.0.0/0']}}
    return {'ingress': result, 'rules': result, 'egress': egress, 'public_ingress': public,
            'private_ingress': private_rules, 'public_rules': public_rules,
            'outbound_rules': [{'protocol': protocol, **({'port_range': '1-65535'} if protocol != 'icmp' else {}), 'destination_addresses': ['0.0.0.0/0', '::/0']} for protocol in ['tcp', 'udp', 'icmp']]}


def provider_request(opts, stage, request, shared=None):
    """Resolve a reviewed recipe; no package owns provider mappings or templates."""
    recipes = json.loads(files('colors_compute').joinpath('provider-recipes.json').read_text())
    provider = opts.get('provider-compute') if isinstance(opts, dict) else None
    if not isinstance(provider, str) or provider not in recipes:
        _fail('compute provider recipe unavailable')
    recipe = recipes[provider]
    endpoint = request.get('endpoint') if isinstance(request, dict) else None
    if isinstance(request, dict) and 'endpoint' in request:
        if not _fields(endpoint, ('kind', 'assignment')) or endpoint != {'kind': 'reserved-ip', 'assignment': 'application'}:
            _fail('invalid compute endpoint request')
        if not recipe.get('application_reserved_ip'):
            _fail('unsupported compute endpoint capability')
    if stage not in ('shared', 'node'):
        _fail('unsupported compute request stage')
    entry = registry()['compute'][provider]
    if not _safe(opts.get('profile')):
        _fail('invalid compute profile')
    if not _fields(request, ('node_id', 'key', 'network', 'security'), ('name', 'endpoint')) or not _safe(request['node_id']):
        _fail('invalid compute request')
    profile = opts['profile']
    name = request.get('name', profile if stage == 'shared' else profile + '-' + request['node_id'])
    if not _safe(name):
        _fail('invalid compute name')
    key, network, security = request['key'], request['network'], request['security']
    if not _fields(key, ('mode',), ('public_key', 'ids', 'reference')) or key['mode'] not in ('managed', 'external'):
        _fail('invalid compute key request')
    if not _fields(network, ('mode',), ('cidr', 'subnet_cidr', 'zone', 'private_ip')):
        _fail('invalid compute network request')
    if network['mode'] != recipe['network_mode']:
        _fail('unsupported compute network mode')
    if not _fields(security, ('ingress', 'egress', 'private_filter')) or security['egress'] != 'all' or type(security['private_filter']) is not bool:
        _fail('unsupported compute security policy')
    if security['private_filter'] and not recipe['private_filter']:
        _fail('unsupported compute private filtering')
    if not isinstance(security['ingress'], list) or not security['ingress']:
        _fail('invalid compute ingress')
    seen, has_ssh = set(), False
    for rule in security['ingress']:
        if not _fields(rule, ('id', 'protocol', 'from_port', 'to_port', 'sources')) or not _safe(rule['id']) or rule['id'] in seen:
            _fail('invalid compute ingress')
        seen.add(rule['id'])
        if not (rule['protocol'] == 'icmp' and rule['from_port'] is None and rule['to_port'] is None or rule['protocol'] in ('tcp', 'udp') and _integer(rule['from_port'], 1, 65535) and _integer(rule['to_port'], int(rule['from_port']), 65535)):
            _fail('invalid compute ingress')
        if not isinstance(rule['sources'], list) or not rule['sources'] or any(not isinstance(source, str) for source in rule['sources']) or len(set(rule['sources'])) != len(rule['sources']):
            _fail('invalid compute ingress')
        for source in rule['sources']:
            if source == 'private':
                if recipe['firewall_format'] == 'hcloud':
                    _fail('unsupported compute private filtering')
            else:
                _cidr(source)
                has_ssh |= rule['protocol'] == 'tcp' and rule['from_port'] <= 22 <= rule['to_port']
    if not has_ssh:
        _fail('compute public SSH ingress required')
    cidr = network.get('cidr')
    if _missing(cidr):
        cidr = next((opts.get(key) for key in recipe['network_cidr_options'] if not _missing(opts.get(key))), None)
    subnet = network.get('subnet_cidr')
    if _missing(subnet):
        subnet = next((opts.get(key) for key in recipe['subnet_cidr_options'] if not _missing(opts.get(key))), cidr)
    parsed = _cidr(cidr) if cidr is not None else None
    if subnet is not None:
        parsed_subnet = _cidr(subnet)
        if parsed is not None and not parsed_subnet.subnet_of(parsed):
            _fail('compute subnet must be inside network')
    if not _missing(network.get('private_ip')):
        if not recipe.get('static_private_ip', False):
            _fail('unsupported compute static private address')
        if not isinstance(network['private_ip'], str):
            _fail('invalid compute private address')
        try:
            address = ipaddress.ip_address(network['private_ip'])
        except ValueError:
            _fail('invalid compute private address')
        if subnet is None or address not in _cidr(subnet):
            _fail('invalid compute private address')
    if type(opts.get('compute-prevent-destroy', True)) is not bool:
        _fail('invalid compute prevent-destroy flag')
    shared = {} if shared is None else shared
    if not isinstance(shared, dict):
        _fail('invalid compute shared results')
    if stage == 'node' and (not isinstance(shared.get('params'), dict) or shared['params'].get('provider') != provider):
        _fail('compute shared provider mismatch')
    registration_owned = key['mode'] == 'managed' or recipe.get('registration_external', False)
    primary = shared.get('ssh_key_id') if registration_owned else key.get('reference')
    ids = [primary] if registration_owned and primary is not None else key.get('ids', [])
    if not isinstance(ids, list) or any(not ((isinstance(item, str) and not _missing(item)) or _integer(item, 1, 9007199254740991)) for item in ids):
        _fail('invalid compute key references')
    if stage == 'node' and entry['registration'] and not ids:
        _fail('compute key references required')
    derived = {'endpoint_count': 1 if endpoint is not None else 0, 'profile': profile, 'name': name, 'node_id': request['node_id'],
        'prevent_destroy': opts.get('compute-prevent-destroy', True), 'public_key': key.get('public_key'),
        'ssh_key_id': primary, 'ssh_key_ids': ids, 'key_name': shared.get('key_name', key.get('reference')),
        'user': entry['user'], 'sudoer': entry['sudoer'], 'network_cidr': cidr, 'subnet_cidr': subnet,
        'network_address': str(parsed.network_address) if parsed else None, 'network_prefix': parsed.prefixlen if parsed else None,
        'network_name': profile + '-network', 'subnet_name': profile + '-subnet', 'firewall_name': profile + '-firewall',
        'network_tag': profile + '-network', 'deployment_tag': 'colors-compute-' + profile,
        'nic_name': name + '-nic', 'public_ip_name': name + '-public'}
    derived.update(_rules(recipe['firewall_format'], request, cidr, profile))
    image = opts.get('google-image-id')
    if _missing(image) and not _missing(opts.get('google-image-project')) and not _missing(opts.get('google-image-family')):
        image = 'projects/' + opts['google-image-project'] + '/global/images/family/' + opts['google-image-family']
    derived['google_image'] = image
    selected_stage = 'shared-keygen' if stage == 'shared' and registration_owned and entry['registration'] else stage
    if stage == 'node' and recipe.get('discovery_stage') and _missing(opts.get(recipe['image_option'])):
        selected_stage = recipe['discovery_stage']
    templates = json.loads(files('colors_compute').joinpath('templates.json').read_text())[provider][selected_stage]
    tokens = sorted(set(re.findall(r'\{\{([a-z_]+)\}\}', json.dumps(templates))))
    context = {'opts': opts, 'request': request, 'shared': shared, 'derived': derived}
    inputs = {}
    for token in tokens:
        if token not in recipe['bindings']:
            _fail('missing provider recipe binding: ' + token)
        spec = recipe['bindings'][token]
        # Null primary-key references are intentional in external/content modes.
        if token == 'ssh_key_id':
            inputs[token] = deepcopy(primary)
        else:
            inputs[token] = _binding(spec, context)
    def check_literals(value):
        if isinstance(value, str) and ('${' in value or '%{' in value):
            _fail('invalid compute literal')
        if isinstance(value, dict):
            for key, item in value.items():
                check_literals(key)
                check_literals(item)
        elif isinstance(value, list):
            for item in value:
                check_literals(item)
    check_literals(inputs)
    return {'provider': provider, 'stage': selected_stage, 'inputs': inputs,
            'documents': provider_plan(provider, selected_stage, inputs)}
