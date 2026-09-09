import json
from pathlib import Path
import pytest
from colors_compute.key_request import key_request

FIXTURES = json.loads((Path(__file__).parents[2] / 'test/fixtures/provider-requests.json').read_text())
PUBLIC = FIXTURES[0]['args'][2]['key']['public_key']


def test_external_public_file_uses_home_and_leaves_files(tmp_path):
    path = tmp_path / 'operator.pub'
    path.write_text(PUBLIC)
    opts = {'provider-compute': 'aws'}
    result = key_request(opts, {'mode': 'external', 'reference': '~/operator.pub'}, {'HOME': str(tmp_path)})
    assert result == {'mode': 'external', 'public_key': PUBLIC}
    assert path.read_text() == PUBLIC
    with pytest.raises(ValueError, match='regular .pub'):
        key_request(opts, {'mode': 'external', 'reference': '~/operator'}, {'HOME': str(tmp_path)})


def test_ids_and_content_have_distinct_semantics():
    assert key_request({'provider-compute': 'hcloud'}, {'mode': 'external', 'reference': [123, 'operator']}) == {'mode': 'external', 'ids': [123, 'operator'], 'reference': 123}
    assert key_request({'provider-compute': 'yandex'}, {'mode': 'external', 'reference': PUBLIC})['public_key'] == PUBLIC
    with pytest.raises(ValueError, match='public key'):
        key_request({'provider-compute': 'yandex'}, {'mode': 'external', 'reference': '/file.pub'})


def test_planning_never_reads_supplied_path():
    result = key_request({'provider-compute': 'azure', 'blue/event': 'build'}, {'mode': 'external', 'reference': '/missing.pub'})
    assert 'PLACEHOLDER' in result['public_key']
