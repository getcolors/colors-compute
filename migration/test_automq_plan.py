import copy
import json
import unittest
from automq_plan import plan, PROVIDER


def resource(kind, name, indices=(None,)):
    return {'mode': 'managed', 'type': kind, 'name': name, 'provider': PROVIDER,
            'instances': [dict({'attributes': {'id': f'{kind}/{name}/{i}', 'password': 'DO-NOT-EMIT', 'private_key': 'DO-NOT-EMIT'}},
                               **({'index_key': i} if i is not None else {})) for i in indices]}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.state = {'version': 4, 'outputs': {'password': {'value': 'DO-NOT-EMIT'}}, 'resources': [
            resource('vultr_vpc', 'cluster'), resource('vultr_ssh_key', 'machine'),
            resource('vultr_firewall_group', 'cluster'),
            resource('vultr_firewall_rule', 'ssh', ('192.0.2.1/32',)),
            resource('vultr_firewall_rule', 'cluster_internal', ('9093', '9094')),
            resource('vultr_instance', 'node', (0, 1, 2))]}
        self.mapping = {f'vultr_instance.node[{i}]': str(i) for i in range(3)}

    def test_complete_read_only_mapping(self):
        original = copy.deepcopy(self.state)
        result = plan(self.state, 'sample', self.mapping)
        self.assertTrue(result['ready_for_review'])
        self.assertFalse(result['executable'])
        self.assertEqual(result['mapped_count'], 9)
        node = next(x for x in result['moves'] if x.get('node_id') == '2')
        self.assertEqual(node['destination_state_key'], 'sample/compute/nodes/2.tfstate')
        self.assertEqual(node['destination_address'], 'vultr_instance.node')
        self.assertNotIn('DO-NOT-EMIT', json.dumps(result))
        self.assertEqual(original, self.state)

    def test_refuses_missing_identity_mapping(self):
        result = plan(self.state, 'sample', {})
        self.assertFalse(result['ready_for_review'])
        self.assertIn('missing node identity mapping: vultr_instance.node[0]', result['errors'])

    def test_duplicate_destination(self):
        self.mapping['vultr_instance.node[1]'] = '0'
        self.assertTrue(any('duplicate destination' in x for x in plan(self.state, 'sample', self.mapping)['errors']))

    def test_duplicate_resource_id_and_source(self):
        instances = self.state['resources'][-1]['instances']
        instances[1]['attributes']['id'] = instances[0]['attributes']['id']
        self.assertTrue(any('duplicate provider resource ownership' in x for x in plan(self.state, 'sample', self.mapping)['errors']))
        instances.append(copy.deepcopy(instances[0]))
        self.assertTrue(any('duplicate source' in x for x in plan(self.state, 'sample', self.mapping)['errors']))

    def test_unknown_resource_and_provider(self):
        self.state['resources'].append(resource('vultr_block_storage', 'volume'))
        self.state['resources'][0]['provider'] = 'unexpected secret binding'
        result = plan(self.state, 'sample', self.mapping)
        self.assertTrue(any('unmapped resource' in x for x in result['errors']))
        self.assertTrue(any('unsupported provider' in x for x in result['errors']))
        self.assertNotIn('unexpected secret binding', json.dumps(result))

    def test_deposed_and_module_refused(self):
        self.state['resources'][0]['instances'][0]['deposed'] = 'opaque'
        self.state['resources'][1]['module'] = 'module.legacy'
        self.assertEqual(plan(self.state, 'sample', self.mapping)['error_count'], 2)

    def test_bad_input_and_unused_mapping(self):
        with self.assertRaises(ValueError):
            plan(self.state, '../unsafe', self.mapping)
        with self.assertRaises(ValueError):
            plan({'values': {}}, 'safe', self.mapping)
        self.mapping['vultr_instance.node[9]'] = '9'
        self.assertIn('node mapping contains unused source addresses', plan(self.state, 'sample', self.mapping)['errors'])


if __name__ == '__main__':
    unittest.main()
