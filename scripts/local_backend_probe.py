#!/usr/bin/env python3
"""Exercise persistent local state and cross-color journal contention without cloud access."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def drivers(directory):
    blue = directory / 'driver.py'
    blue.write_text('''import asyncio, json, sys
from colors_compute.backend import read_state
from colors_compute.execution import state_presence, converge_state
from colors_compute.journal import journal_get, journal_put
case = json.load(sys.stdin)
print(json.dumps(asyncio.run(globals()[case['op']](*case['args']))))
''')
    red = directory / 'driver.ts'
    red.write_text(f'''import {{readState}} from {json.dumps(str(ROOT / 'red/src/backend.ts'))};
import {{statePresence,convergeState}} from {json.dumps(str(ROOT / 'red/src/execution.ts'))};
import {{journalGet,journalPut}} from {json.dumps(str(ROOT / 'red/src/journal.ts'))};
const operations = {{read_state:readState,state_presence:statePresence,converge_state:convergeState,journal_get:journalGet,journal_put:journalPut}};
const c=JSON.parse(await Bun.stdin.text());
console.log(JSON.stringify(await operations[c.op](...c.args)));
''')
    green = directory / 'driver.clj'
    green.write_text('''(require '[cheshire.core :as json]
 '[io.github.getcolors.compute-runtime :as runtime]
 '[io.github.getcolors.compute-execution :as execution]
 '[io.github.getcolors.compute-journal :as journal])
(let [{:keys [op args]} (json/parse-string (slurp *in*) true)
      operations {"read_state" runtime/read-state "state_presence" execution/state-presence
                  "converge_state" execution/converge-state "journal_get" journal/journal-get "journal_put" journal/journal-put}]
 (println (json/generate-string (apply (get operations op) args))))
''')
    return {
        'blue': [str(ROOT / 'blue/.venv/bin/python'), str(blue)],
        'red': [os.environ.get('BUN', 'bun'), str(red)],
        'green': [os.environ.get('BB', 'bb'), '-cp',
                  str(ROOT / 'green/src/clj') + ':' + str(ROOT / 'green/src/resources'), str(green)],
    }


def main():
    tofu = shutil.which(os.environ.get('TOFU', 'tofu'))
    if not tofu:
        raise SystemExit('OpenTofu is required')
    with tempfile.TemporaryDirectory(prefix='colors-compute-local-probe-') as temp:
        directory = Path(temp)
        commands = drivers(directory)
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith(('TF_', 'TOFU_', 'COLORS_PAR_'))}
        environment['PYTHONPATH'] = str(ROOT / 'blue/src')
        environment.update(TF_IN_AUTOMATION='1', TF_INPUT='0', TF_WORKSPACE='default')
        default = '--default' in sys.argv
        if default:
            environment['HOME'] = str(directory / 'home')
        state_root = directory / 'home/.local/state/colors' if default else directory / 'states'
        opts = {'profile': 'demo', 'provider-compute': 'vultr',
                'provider-backend': 'local', **({} if default else {'local-state-dir': str(state_root)})}

        def call(color, op, *args):
            process = subprocess.run(commands[color], input=json.dumps({'op': op, 'args': args}),
                                     text=True, capture_output=True, cwd=ROOT,
                                     env=environment, timeout=120, check=True)
            return json.loads(process.stdout)

        key = 'demo/compute/shared.tfstate'
        state = state_root / key
        for color in commands:
            assert call(color, 'state_presence', opts, key) == {'status': 'absent'}, color
            assert call(color, 'read_state', opts, key) == {'status': 'error'}, color
        state.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        module = directory / 'module'
        module.mkdir()
        (module / 'main.tf.json').write_text(json.dumps({
            'terraform': {'backend': {'local': {'path': str(state)}}},
            'output': {'params': {'value': {'probe': 'persistent-local-state'}}},
        }))
        for argv in (['init', '-input=false', '-no-color'],
                     ['apply', '-auto-approve', '-input=false', '-no-color']):
            subprocess.run([tofu, *argv], cwd=module, env=environment,
                           capture_output=True, text=True, timeout=120, check=True)
        shutil.rmtree(module)
        original = state.read_bytes()
        for color in commands:
            assert call(color, 'state_presence', opts, key) == {'status': 'present'}, color
            assert call(color, 'read_state', opts, key) == {
                'status': 'present', 'params': {'probe': 'persistent-local-state'}}, color
            assert state.read_bytes() == original, color
        print('All colors read native OpenTofu state after its working directory was removed')

        for color in commands:
            execution_opts = {**opts, 'profile': color, 'provider-compute': 'aws',
                              'compute-prevent-destroy': False}
            execution_key = color + '/compute/shared.tfstate'
            documents = {'main.tf.json': {'output': {'params': {'value': {'provider': 'aws'}}}}}
            ready = call(color, 'converge_state', execution_opts, execution_key,
                         documents, 'create', {'status': 'absent'})
            assert ready.get('status') == 'ready', (color, ready)
            persisted = state_root / execution_key
            assert persisted.is_file() and persisted.stat().st_mode & 0o777 == 0o600, color
            checked = call(color, 'converge_state', execution_opts, execution_key,
                           documents, 'check', {'status': 'present'})
            assert checked.get('status') == 'clean', (color, checked)
            deleted = call(color, 'converge_state', execution_opts, execution_key,
                           documents, 'delete', {'status': 'present'})
            assert deleted == {'status': 'destroyed'}, (color, deleted)
            assert persisted.is_file(), color
        print('All colors create, check, and destroy an output-only plan with native OpenTofu')

        identity = {'profile': 'demo', 'provider': 'vultr',
                    'backend': {'kind': 'local', 'path': str(state_root)}}
        document = {'schema_version': 1, 'identity': identity, 'revision': 1,
                    'write_id': 'write-1', 'lock': {'state': 'held', 'run_id': 'run-1'},
                    'topology_declared': False, 'nodes': {}}
        create = {'condition': {'if_none_match': '*'}, 'document': document}
        for color in commands:
            assert call(color, 'journal_get', opts) == {'status': 'absent'}, color
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda color: call(color, 'journal_put', opts, create), commands))
        assert sorted(result['status'] for result in results) == ['conflict', 'conflict', 'written'], results
        journal = state_root / 'demo/compute/coordination.json'
        etag = hashlib.sha256(journal.read_bytes()).hexdigest()
        for index, color in enumerate(commands, 2):
            assert call(color, 'journal_get', opts) == {
                'status': 'present', 'etag': etag, 'document': document}, color
            assert call(color, 'journal_put', opts, create) == {'status': 'conflict'}, color
            document = {**document, 'revision': index, 'write_id': f'write-{index}'}
            update = {'condition': {'if_match': etag}, 'document': document}
            written = call(color, 'journal_put', opts, update)
            assert written['status'] == 'written', (color, written)
            assert call(color, 'journal_put', opts, update) == {'status': 'conflict'}, color
            etag = hashlib.sha256(journal.read_bytes()).hexdigest()
            assert written['etag'] == etag
        assert journal.stat().st_mode & 0o777 == 0o600
        print('All colors share journal ETags; competing creates and stale updates are refused')

        lock = Path(str(journal) + '.lock')
        lock.mkdir(mode=0o700)
        for color in commands:
            assert call(color, 'journal_put', opts, {
                'condition': {'if_match': etag}, 'document': document}) == {'status': 'conflict'}, color
        lock.rmdir()
        for content in (b'{broken', b'[]', b'\xff', b'\xef\xbb\xbf{}', b' ' * (2 * 1024 * 1024 + 1)):
            journal.write_bytes(content)
            for color in commands:
                assert call(color, 'journal_get', opts) == {'status': 'error'}, (color, len(content))
                assert call(color, 'journal_put', opts, create) == {'status': 'error'}, (color, len(content))
            assert journal.read_bytes() == content
        state.unlink()
        state.symlink_to(directory / 'missing.tfstate')
        for color in commands:
            assert call(color, 'journal_get', opts) == {'status': 'error'}, color
            assert call(color, 'state_presence', opts, key) == {'status': 'error'}, color
        print('Held journal locks, corrupt journals, and dangling state symlinks fail closed')


if __name__ == '__main__':
    main()
