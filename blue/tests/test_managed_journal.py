from copy import deepcopy
import json
from pathlib import Path
import pytest
from colors_compute.managed_journal import managed_coordination,managed_document_valid
CASES=json.loads((Path(__file__).parents[2]/'test/fixtures/managed-journal.json').read_text())
@pytest.mark.parametrize('case',CASES,ids=lambda c:c['name'])
def test_managed_contract(case):
    args=deepcopy(case['args']);before=deepcopy(args)
    if 'error' in case:
        with pytest.raises(ValueError,match=case['error']):managed_coordination(*args)
    else:
        result=managed_coordination(*args);assert result==case['expected'];assert managed_document_valid(result['document'])
        assert 'key' not in result['document'] and 'nodes' not in result['document']
    assert args==before
