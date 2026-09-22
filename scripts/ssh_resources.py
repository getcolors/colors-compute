"""Package the same audited SSH process adapter into each color."""
import argparse
from pathlib import Path
root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--write', action='store_true')
args = p.parse_args()
source = (root / 'agents/ssh-resource.py').read_bytes()
for directory in ('blue/src/colors_compute', 'red/resources', 'green/src/resources/colors_compute'):
    path = root / directory / 'ssh_adapter.py'
    if args.write:
        path.write_bytes(source)
        path.chmod(0o755)
    if not path.exists() or path.read_bytes() != source:
        raise SystemExit('SSH adapter resource drift: ' + str(path))
print('SSH adapter copies: passed')
