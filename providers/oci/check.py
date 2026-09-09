"""Check canonical OCI renders and optionally validate provider schema in temporary dirs."""
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
    for mode in ('shared', 'node-pinned', 'node-discovery'):
        templates = {'main.tf.json': 'shared' if mode == 'shared' else 'node'}
        if mode != 'shared':
            templates['image.tf.json'] = 'node-image-pinned' if mode == 'node-pinned' else 'node-image-discovery'
        for output, template in templates.items():
            source = json.loads((ROOT / f'{template}.tf.json.template').read_text())
            rendered = render(source, inputs)
            expected = (ROOT / 'examples' / mode / output).read_text()
            assert json.dumps(rendered, indent=2) + '\n' == expected, f'render drift: {mode}/{output}'
            assert 'COLORS_PAR_' not in expected and 'private_key' not in expected
            if output == 'main.tf.json':
                assert rendered['provider']['oci'] == {'config_file_profile': 'DEFAULT'}
                if mode != 'shared':
                    assert set(rendered['resource']) == {'oci_core_instance'}
                    instance = rendered['resource']['oci_core_instance']['node']
                    assert 'count' not in instance
                    assert instance['shape_config']['ocpus'] == 1
                    assert instance['create_vnic_details']['nsg_ids'] == inputs['nsg_ids']
                else:
                    assert 'oci_core_instance' not in rendered['resource']
                    assert 'oci_core_subnet' not in rendered['resource']
    assert render('{{value}}', {'value': True}) is True
    assert render('{{value}}', {'value': 24}) == 24
    assert render('{{value}}', {'value': ['a']}) == ['a']
    assert render('${oci_core_instance.node.id}', {}) == '${oci_core_instance.node.id}'
    for value in ('prefix{{value}}', '{{missing}}'):
        try:
            render(value, {})
        except ValueError:
            pass
        else:
            raise AssertionError('missing/partial token accepted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tofu', type=Path, help='also initialize and validate all examples with this binary')
    args = parser.parse_args()
    check()
    print('Deterministic render contract: passed', flush=True)
    if args.tofu:
        # Schema checks need registry access but no ambient provider/backend credentials.
        env = {key: value for key, value in os.environ.items()
               if key in ('PATH', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NIX_SSL_CERT_FILE', 'LANG')}
        for mode in ('shared', 'node-pinned', 'node-discovery'):
            with tempfile.TemporaryDirectory(prefix='colors-compute-oci-') as directory:
                env['HOME'] = directory
                for source in (ROOT / 'examples' / mode).glob('*.tf.json'):
                    (Path(directory) / source.name).write_bytes(source.read_bytes())
                for command in (('fmt', '-check'), ('init', '-backend=false', '-input=false', '-no-color'), ('validate', '-no-color')):
                    subprocess.run([str(args.tofu.resolve()), f'-chdir={directory}', *command], env=env, check=True)
                print(f'Provider schema {mode}: passed', flush=True)


if __name__ == '__main__':
    main()
