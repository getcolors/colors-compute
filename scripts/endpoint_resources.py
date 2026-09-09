"""Package the standalone endpoint adapter into each independent library."""
import argparse
from pathlib import Path
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
source = (root / 'agents/endpoint-agent.py').read_bytes()
for directory in ('blue/src/colors_compute','red/resources','green/src/resources/colors_compute'):
    path = root / directory / 'endpoint-agent.py'
    if args.write:
        path.write_bytes(source)
    if not path.exists() or path.read_bytes() != source:
        raise SystemExit('endpoint agent resource drift: ' + str(path))
print('Endpoint agent copies: passed')
