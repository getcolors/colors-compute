"""Validate the reserved endpoint schema without credentials, backend, plan, or apply."""
import json,os,subprocess,tempfile,sys
from pathlib import Path
cases=json.loads((Path(__file__).parent/'fixtures/provider-endpoint.json').read_text())
case=next(case for case in cases if case['name']=='digitalocean-endpoint')
tofu=str(Path(sys.argv[1]).resolve())
env={k:v for k,v in os.environ.items() if k in ('HOME','PATH','SSL_CERT_FILE','SSL_CERT_DIR','NIX_SSL_CERT_FILE','LANG')}
with tempfile.TemporaryDirectory(prefix='compute-endpoint-schema-') as d:
 for name,doc in case['expected']['documents'].items():Path(d,name).write_text(json.dumps(doc))
 for args in [('init','-backend=false','-input=false','-no-color'),('validate','-no-color')]:
  r=subprocess.run([tofu,*args],cwd=d,env=env,capture_output=True,text=True,timeout=240)
  if r.returncode:raise RuntimeError(r.stdout+r.stderr)
 print('DigitalOcean reserved endpoint schema: passed')
