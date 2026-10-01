"""Real OpenSSH interoperability across all three native SSH APIs; no cloud calls."""
import json
import importlib.util
import fcntl
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
BLUE = """
import asyncio,json,os,sys
sys.path.insert(0,'blue/src')
from colors_compute.ssh import ssh_resource,start_agent,ssh_export
async def main():
 m=json.load(sys.stdin)
 if m['operation']=='agent':
  cleanups=[]
  try:
   a=await start_agent([{'opts':m['opts'],'request':m['request'],'resource':m['resource']}],dict(os.environ),lambda phase,fn:cleanups.append(fn))
  finally:
   for close in cleanups:await close()
  print(json.dumps({'ready':a['status']=='ready','count':len(a['identities']),'closed':not os.path.exists(a['socket'])}))
 elif m['operation']=='export':print(json.dumps(await ssh_export(m['opts'],m['request'],m['destination'],m['export_operation'])))
 else:print(json.dumps(await ssh_resource(m['opts'],m['request'],m['operation'])))
asyncio.run(main())
"""
RED = """
import {readFileSync,existsSync} from 'node:fs';
import {ssh_resource,start_agent,ssh_export} from './red/src/ssh.ts';
const m=JSON.parse(readFileSync(0,'utf8'));
if(m.operation==='agent'){
 const cleanups=[]; let a;
 try{a=await start_agent([{opts:m.opts,request:m.request,resource:m.resource}],process.env,(phase,fn)=>cleanups.push(fn));}
 finally{for(const close of cleanups)await close();}
 console.log(JSON.stringify({ready:a.status==='ready',count:Object.keys(a.identities).length,closed:!existsSync(a.socket)}));
}else if(m.operation==='export') console.log(JSON.stringify(await ssh_export(m.opts,m.request,m.destination,m.export_operation)));
else console.log(JSON.stringify(await ssh_resource(m.opts,m.request,m.operation)));
"""
GREEN = """
(require '[cheshire.core :as json] '[clojure.java.io :as io] '[io.github.getcolors.compute-ssh :as ssh])
(let [m (json/parse-string (slurp *in*) true)]
 (println (json/generate-string
  (if (= "agent" (:operation m))
   (let [cleanups (atom []) a (try (ssh/start-agent! [{:opts (:opts m) :request (:request m) :resource (:resource m)}] (System/getenv) (fn [_ close] (swap! cleanups conj close))) (finally (doseq [close @cleanups] (close))))]
    {:ready (= "ready" (:status a)) :count (count (:identities a)) :closed (not (.exists (io/file (:socket a))))})
   (if (= "export" (:operation m))
    (ssh/ssh-export! (:opts m) (:request m) (:destination m) (:export_operation m) (System/getenv))
    (ssh/ssh-resource! (:opts m) (:request m) (:operation m) (System/getenv)))))))
"""
COMMANDS = {'blue':[sys.executable,'-c',BLUE], 'red':['bun','-e',RED], 'green':['bb','--classpath','green/src/clj:green/src/resources','-e',GREEN]}
def invoke(color, message, env):
    p = subprocess.run(COMMANDS[color], cwd=ROOT, env=env, input=json.dumps(message), capture_output=True,text=True,timeout=90)
    if p.returncode: raise RuntimeError(color + ' native invocation failed: ' + p.stderr[:500])
    value=json.loads(p.stdout)
    if value.get('status')=='error': raise RuntimeError(color + ' SSH operation failed: ' + json.dumps(value))
    return value
with tempfile.TemporaryDirectory() as tmp:
    env=dict(os.environ, COLORS_PAR_INTEROP_OLD=secrets.token_urlsafe(40)+' $`" ☃',COLORS_PAR_INTEROP_NEW=secrets.token_urlsafe(40))
    opts={'profile':'interoperability','provider-backend':'local'}
    request={'name':'app-access','workdir':str(Path(tmp).resolve()),'passphrase_env':'COLORS_PAR_INTEROP_OLD'}
    message={'opts':opts,'request':request,'operation':'create'}
    initial=invoke('green',message,env)
    for color in COMMANDS:
        assert invoke(color,message,env)==initial
        assert invoke(color,dict(message,operation='agent',resource=initial),env)=={'ready':True,'count':1,'closed':True}
    destination = str(Path(tmp).resolve() / 'exports' / 'identity')
    export = dict(message, operation='export', destination=destination, export_operation='install')
    installed = invoke('green', export, env)
    assert installed['status'] == 'installed' and installed['fingerprint'] == initial['fingerprint']
    private = Path(installed['private_key_file'])
    original_ciphertext = private.read_bytes()
    original_manifest = private.with_name('ownership.json').read_bytes()
    assert b'OPENSSH PRIVATE KEY' in original_ciphertext
    for color in COMMANDS:
        assert invoke(color, export, env) == installed
        assert invoke(color, dict(export, export_operation='inspect'), dict(os.environ)) == installed
    # Unowned directories, linked files and altered identity are never adopted.
    unowned = Path(tmp).resolve() / 'unowned'
    unowned.mkdir(mode=0o700)
    def refused(message):
        try: invoke('blue', message, env)
        except RuntimeError: return
        raise AssertionError('unsafe export accepted')
    refused(dict(export, destination=str(unowned)))
    lock_fd = os.open(Path(destination).parent, os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused(dict(export, export_operation='inspect'))
    finally:
        os.close(lock_fd)
    refused(dict(export, export_operation='inspect', request=dict(request, expected=dict(initial, fingerprint='SHA256:wrong'))))
    private.write_bytes(original_ciphertext + b'\n')
    refused(dict(export, export_operation='remove'))
    private.write_bytes(original_ciphertext)
    linked = private.with_name('hardlink')
    os.link(private, linked)
    refused(dict(export, export_operation='remove'))
    linked.unlink()
    public_path = Path(installed['public_key_file'])
    public_bytes = public_path.read_bytes()
    public_path.unlink()
    public_path.symlink_to(private)
    refused(dict(export, export_operation='inspect'))
    public_path.unlink()
    public_path.write_bytes(public_bytes)
    public_path.chmod(0o600)
    rotated=invoke('red',dict(message,operation='rotate',request=dict(request,new_passphrase_env='COLORS_PAR_INTEROP_NEW')),env)
    assert rotated==initial
    message['request']=dict(request,passphrase_env='COLORS_PAR_INTEROP_NEW')
    for color in COMMANDS:
        assert invoke(color,dict(message,operation='agent',resource=initial),env)=={'ready':True,'count':1,'closed':True}
    # Simulate interruption after each atomic rotation write. Both old/new
    # ciphertext states remain inspectable and safely retryable.
    spec = importlib.util.spec_from_file_location('ssh_adapter_test', ROOT / 'agents/ssh-resource.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    sys.path.insert(0, str(ROOT / 'blue/src'))
    from colors_compute.ssh import ssh_plan
    plan = ssh_plan(opts, request)
    original_write = adapter.atomic_write
    for fail_after in (1, 2, 3):
        private.write_bytes(original_ciphertext)
        private.with_name('ownership.json').write_bytes(original_manifest)
        writes = [0]
        def interrupted_write(path, data):
            original_write(path, data)
            writes[0] += 1
            if writes[0] == fail_after:
                raise RuntimeError('simulated interruption')
        adapter.atomic_write = interrupted_write
        try:
            adapter.export_resource(plan, request, destination, 'install')
            raise AssertionError('interruption not reached')
        except RuntimeError:
            pass
        finally:
            adapter.atomic_write = original_write
        assert adapter.export_resource(plan, request, destination, 'inspect') == installed
        assert invoke('green', export, env) == installed
    for color in COMMANDS:
        assert invoke(color, export, env) == installed
    assert private.read_bytes() != original_ciphertext
    # Local inspection/removal still works after remote authority is removed.
    deleted=invoke('blue',dict(message,operation='delete',request=dict(request,allow_delete=True,consumers_destroyed=True)),env)
    assert deleted['status']=='destroyed'
    assert invoke('red', dict(export, export_operation='inspect'), dict(os.environ)) == installed
    assert invoke('blue', dict(export, export_operation='remove'), dict(os.environ))['status'] == 'removed'
    for color in COMMANDS:
        assert invoke(color, dict(export, export_operation='inspect'), dict(os.environ))['status'] == 'absent'
    assert not Path(destination).exists()

print('Native SSH lifecycle: all-color resources/agents/exports, interrupted rotation, unsafe paths and offline removal passed')
