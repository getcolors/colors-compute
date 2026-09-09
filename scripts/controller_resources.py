#!/usr/bin/env python3
"""Package library-owned Kubernetes controller task artifacts."""
import argparse
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
COPIES=['blue/src/colors_compute/controllers.json','red/resources/controllers.json','green/src/resources/colors_compute/controllers.json']

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--write',action='store_true');args=parser.parse_args()
    sources=json.loads((ROOT/'contracts/controllers.json').read_text())
    content=json.dumps({p:{**{k:v for k,v in descriptor.items() if k!='template'},'content':(ROOT/descriptor['template']).read_text()} for p,descriptor in sources.items()},indent=2,sort_keys=True)+'\n'
    for relative in COPIES:
        path=ROOT/relative
        if args.write:path.write_text(content)
        if not path.exists() or path.read_text()!=content:raise SystemExit('controller resource drift: '+relative)
    print('Controller resources: passed')
if __name__=='__main__':main()
