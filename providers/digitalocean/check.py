"""Check canonical DigitalOcean renders and optionally validate provider schema in temporary dirs."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
TOKEN = re.compile(r'\{\{([a-z_]+)\}\}\Z')


def render(value, inputs):
    if isinstance(value, str):
        match = TOKEN.fullmatch(value)
        if match:
            if match[1] not in inputs:
                raise ValueError(f'missing template input: {match[1]}')
            return copy.deepcopy(inputs[match[1]])
        if '{{' in value or '}}' in value:
            raise ValueError('template placeholders must occupy the entire string')
        return value
    if isinstance(value, dict):
        return {key: render(item, inputs) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, inputs) for item in value]
    return value


def check():
    inputs = json.loads((ROOT / 'examples/inputs.json').read_text())
    for mode in ('shared-keygen', 'shared-optout', 'node'):
        templates = {'node.tf.json': 'node'} if mode == 'node' else {'shared.tf.json': 'shared'}
        if mode == 'shared-keygen':
            templates['shared-keygen.tf.json'] = 'shared-keygen'
        for output, template in templates.items():
            source = json.loads((ROOT / f'{template}.tf.json.template').read_text())
            rendered = render(source, inputs)
            expected = (ROOT / 'examples' / mode / output).read_text()
            assert json.dumps(rendered, indent=2) + '\n' == expected, f'render drift: {mode}/{output}'
            assert 'COLORS_PAR_' not in expected and 'api_key' not in expected
    shared = json.loads((ROOT / 'examples/shared-keygen/shared.tf.json').read_text())
    firewall = shared['resource']['digitalocean_firewall']['network']
    assert 'droplet_ids' not in firewall
    assert firewall['tags'] == ['${digitalocean_tag.deployment.name}']
    assert 'digitalocean_vpc' not in shared['resource']
    node = json.loads((ROOT / 'examples/node/node.tf.json').read_text())
    assert inputs['deployment_tag'] in node['resource']['digitalocean_droplet']['node']['tags']
    assert render('{{value}}', {'value': True}) is True
    assert render('{{value}}', {'value': 24}) == 24
    assert render('{{value}}', {'value': ['a']}) == ['a']
    assert render('${digitalocean_droplet.node.id}', {}) == '${digitalocean_droplet.node.id}'
    for value in ('prefix{{value}}', '{{missing}}'):
        try:
            render(value, {})
        except ValueError:
            pass
        else:
            raise AssertionError('missing/partial token accepted')


def created_role_examples():
    fixtures = json.loads((ROOT.parents[1] / 'test/fixtures/provider-network-created.json').read_text())
    case = next(case for case in fixtures if case['name'] == 'digitalocean-created-role-plan')
    documents = case['expected']['documents']
    stages = {'shared-created-roles': documents['shared']}
    stages.update({'node-' + name: value for name, value in documents['nodes'].items()})
    return {stage: {name: doc for name, doc in docs.items() if name != 'backend.tf.json'}
            for stage, docs in stages.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tofu', type=Path, help='also initialize and validate all examples with this binary')
    args = parser.parse_args()
    check()
    print('Deterministic render contract: passed', flush=True)
    if args.tofu:
        # Schema checks need registry access but no ambient provider/backend credentials.
        env = {key: value for key, value in os.environ.items()
               if key in ('PATH', 'HOME', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NIX_SSL_CERT_FILE', 'LANG')}
        for mode in ('shared-keygen', 'shared-optout', 'node'):
            with tempfile.TemporaryDirectory(prefix='colors-compute-digitalocean-') as directory:
                for source in (ROOT / 'examples' / mode).glob('*.tf.json'):
                    (Path(directory) / source.name).write_bytes(source.read_bytes())
                for command in (('fmt', '-check'), ('init', '-backend=false', '-input=false', '-no-color'), ('validate', '-no-color')):
                    subprocess.run([str(args.tofu.resolve()), f'-chdir={directory}', *command], env=env, check=True)
                print(f'Provider schema {mode}: passed', flush=True)
        for mode, documents in created_role_examples().items():
            with tempfile.TemporaryDirectory(prefix='colors-compute-digitalocean-roles-') as directory:
                for name, document in documents.items():
                    (Path(directory) / name).write_text(json.dumps(document, indent=2) + '\n')
                for command in (('init', '-backend=false', '-input=false', '-no-color'), ('validate', '-no-color')):
                    subprocess.run([str(args.tofu.resolve()), f'-chdir={directory}', *command], env=env, check=True)
                print(f'Provider schema {mode}: passed', flush=True)



if __name__ == '__main__':
    main()
