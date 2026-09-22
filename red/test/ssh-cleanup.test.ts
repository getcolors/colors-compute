import {expect, test} from 'bun:test';
import {start_agent, ssh_resource} from '../src/ssh.ts';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

test('registration rejection closes the helper without an unhandled readiness rejection', async () => {
  const error = new Error('scope closed');
  await expect(start_agent([], process.env, () => {throw error;})).rejects.toBe(error);
  // The adapter also rejects its empty resource list. That secondary readiness
  // rejection must be observed even though registration failed before awaiting it.
  await Bun.sleep(20);
});


test('short-lived adapters drain their protocol response before exit rejection', async () => {
  const workdir = mkdtempSync(join(tmpdir(), 'colors-ssh-exit-test-'));
  try {
    // Inspect emits one failure record and exits immediately. A valid response
    // still must be parsed even when process exit precedes stdout delivery.
    const results = await Promise.all(Array.from({length: 12}, () => ssh_resource(
      {profile: 'exit-test', 'provider-backend': 'local'},
      {name: 'missing', workdir, passphrase_env: 'COLORS_PAR_TEST_KEY'},
      'inspect')));
    expect(results.every(result => result.status === 'error')).toBe(true);
  } finally {rmSync(workdir, {recursive: true, force: true});}
});
