"""Disposable real-OpenSSH benchmark for the explicit 64-round bcrypt policy."""
import asyncio
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blue/src'))
from colors_compute.ssh import ssh_resource, start_agent

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        opts = {'profile':'benchmark', 'provider-backend':'local'}
        env = dict(os.environ, COLORS_PAR_BENCHMARK_KEY=secrets.token_urlsafe(48))
        entries = []
        for i in range(4):
            request = {'name':f'key-{i}', 'workdir':str(Path(tmp).resolve()), 'passphrase_env':'COLORS_PAR_BENCHMARK_KEY'}
            begin = time.monotonic()
            resource = await ssh_resource(opts, request, environment=env)
            if resource.get('status') != 'ready': raise RuntimeError('benchmark generation failed')
            print(f'generate key {i+1}: {time.monotonic()-begin:.3f}s')
            entries.append({'opts':opts, 'request':request, 'resource':resource})
        for count in (1,2,4):
            cleanups = []
            begin = time.monotonic()
            try:
                await start_agent(entries[:count],env,lambda phase,close:cleanups.append(close))
                print(f'load {count} identities: {time.monotonic()-begin:.3f}s')
            finally:
                for close in cleanups: await close()
asyncio.run(main())
