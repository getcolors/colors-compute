import json
from pathlib import Path
import pytest
from colors_compute.provider_request import provider_request
CASES=json.loads((Path(__file__).parents[2]/'test/fixtures/provider-icmp.json').read_text())
@pytest.mark.parametrize('case',CASES,ids=lambda case:case['name'])
def test_icmp(case):
    if 'error' in case['expected']:
        with pytest.raises(ValueError,match=case['expected']['error']):provider_request(*case['args'])
    else:assert provider_request(*case['args'])==case['expected']
