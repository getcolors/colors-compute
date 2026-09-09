"""Read-only AutoMQ raw-state address planner. Never emits state attributes."""
import argparse
import json
import re
import sys
from pathlib import Path

SAFE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}\Z")
SHARED = {
    ('vultr_ssh_key', 'machine'): 'vultr_ssh_key.machine',
    ('vultr_vpc', 'cluster'): 'vultr_vpc.network',
    ('vultr_firewall_group', 'cluster'): 'vultr_firewall_group.network',
}
RULES = {'ssh', 'kafka', 'cluster_internal'}
PROVIDER = 'provider["registry.opentofu.org/vultr/vultr"]'


def plan(state, profile, node_mapping):
    """Explicit mapping is source address -> stable node_id; no identity guessing."""
    if not isinstance(profile, str) or not SAFE.fullmatch(profile):
        raise ValueError('profile must be a safe identifier')
    if not isinstance(state, dict) or state.get('version') != 4 or not isinstance(state.get('resources'), list):
        raise ValueError('expected raw OpenTofu state version 4 with resources')
    if not isinstance(node_mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not SAFE.fullmatch(v) for k, v in node_mapping.items()):
        raise ValueError('node mapping must map source addresses to safe node identifiers')
    errors, moves = [], []
    seen_source, seen_destination, seen_identity, used_mapping = set(), set(), set(), set()
    for resource in state['resources']:
        if not isinstance(resource, dict):
            errors.append('malformed resource'); continue
        kind, name = resource.get('type'), resource.get('name')
        # Only provider/resource identifiers enter diagnostics; reject arbitrary strings.
        if not all(isinstance(x, str) and re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', x) for x in (kind, name)):
            errors.append('invalid resource identifier'); continue
        base = f'{kind}.{name}'
        instances = resource.get('instances')
        if not isinstance(instances, list):
            errors.append(f'malformed instances: {base}'); continue
        for instance in instances:
            if not isinstance(instance, dict):
                errors.append(f'malformed instance: {base}'); continue
            index = instance.get('index_key')
            if index is not None and (isinstance(index, bool) or not isinstance(index, (str, int))):
                errors.append(f'invalid instance index: {base}'); continue
            address = base + (f'[{json.dumps(index, ensure_ascii=True)}]' if index is not None else '')
            if resource.get('module') or resource.get('mode') != 'managed':
                errors.append(f'unsupported module or resource mode: {address}'); continue
            if address in seen_source:
                errors.append(f'duplicate source ownership: {address}'); continue
            seen_source.add(address)
            if instance.get('deposed') or instance.get('status') == 'tainted':
                errors.append(f'deposed or tainted instance requires review: {address}'); continue
            provider = resource.get('provider')
            if provider not in (PROVIDER, 'provider["registry.terraform.io/vultr/vultr"]'):
                errors.append(f'unsupported provider binding: {address}'); continue
            node_id = None
            if (kind, name) == ('vultr_instance', 'node') and isinstance(index, int) and index >= 0:
                node_id = node_mapping.get(address)
                if node_id is None:
                    errors.append(f'missing node identity mapping: {address}'); continue
                used_mapping.add(address)
                target, key = 'vultr_instance.node', f'{profile}/compute/nodes/{node_id}.tfstate'
            elif (kind, name) in SHARED and index is None:
                target, key = SHARED[(kind, name)], f'{profile}/compute/shared.tfstate'
            elif kind == 'vultr_firewall_rule' and name in RULES and isinstance(index, str):
                target = 'vultr_firewall_rule.ingress[' + json.dumps(f'{name}:{index}', ensure_ascii=True) + ']'
                key = f'{profile}/compute/shared.tfstate'
            else:
                errors.append(f'unmapped resource: {address}'); continue
            destination = (key, target)
            if destination in seen_destination:
                errors.append(f'duplicate destination ownership: {address}'); continue
            seen_destination.add(destination)
            # Compare resource IDs internally to detect double ownership, but never expose them.
            attrs = instance.get('attributes')
            resource_id = attrs.get('id') if isinstance(attrs, dict) else None
            if not isinstance(resource_id, str) or not resource_id:
                errors.append(f'missing resource ownership identity: {address}'); continue
            identity = (kind, resource_id)
            if identity in seen_identity:
                errors.append(f'duplicate provider resource ownership: {address}'); continue
            seen_identity.add(identity)
            move = {'source_address': address, 'destination_state_key': key, 'destination_address': target, 'provider': 'vultr'}
            if node_id is not None:
                move['node_id'] = node_id
            moves.append(move)
    if set(node_mapping) - used_mapping:
        errors.append('node mapping contains unused source addresses')
    if not any('node_id' in x for x in moves):
        errors.append('no mapped nodes')
    return {'schema_version': 1, 'package': 'automq', 'provider': 'vultr',
            'ready_for_review': not errors, 'executable': False,
            'mapped_count': len(moves), 'error_count': len(errors),
            'moves': sorted(moves, key=lambda x: x['source_address']), 'errors': sorted(errors)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--node-mapping', required=True, type=Path)
    args = parser.parse_args()
    try:
        # Parsing failures must not echo state or mapping content.
        try:
            state = json.loads(args.state.read_text())
            mapping = json.loads(args.node_mapping.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError('could not read valid input JSON') from None
        result = plan(state, args.profile, mapping)
    except ValueError as error:
        print(json.dumps({'error': str(error), 'executable': False}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['ready_for_review'] else 2


if __name__ == '__main__':
    sys.exit(main())
