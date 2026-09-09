import {test, expect} from 'bun:test';
import {registrationPreflight} from '../src/registration.ts';
const bytes = (value: unknown) => Buffer.from(JSON.stringify(value));
const opts = {profile: 'demo', 'provider-compute': 'digitalocean'};
const env = {COLORS_PAR_DO_TOKEN: 'private-value'};

test('paginated collision is refused and authentication remains in headers', async () => {
  const calls: string[] = [];
  await expect(registrationPreflight(opts, 'managed', null, 'ssh-ed25519 key', env, (url, headers) => {
    calls.push(url);
    expect(url).not.toContain(env.COLORS_PAR_DO_TOKEN);
    expect(headers.Authorization).toBe('Bearer private-value');
    return bytes(calls.length === 1
      ? {ssh_keys: [], links: {pages: {next: 'https://api.digitalocean.com/v2/account/keys?page=2'}}}
      : {ssh_keys: [{id: 1, name: 'demo', public_key: 'ssh-ed25519 key'}]});
  })).rejects.toThrow('unowned SSH registration');
  expect(calls.length).toBe(2);
});

test('foreign next URL never receives credentials', async () => {
  let calls = 0;
  await expect(registrationPreflight(opts, 'managed', null, null, env, () => {
    calls++;
    return bytes({ssh_keys: [], links: {pages: {next: 'https://example.com/steal'}}});
  })).rejects.toThrow('SSH registration preflight failed');
  expect(calls).toBe(1);
});

test('owned ID is confirmed, malformed pages and BOM refused', async () => {
  const ownership = {provider: 'digitalocean', scope: 'account', id: '1'};
  expect(await registrationPreflight(opts, 'managed', ownership, 'ssh-ed25519 key', env, () => bytes({ssh_keys: [{id: 1, name: 'demo', public_key: 'ssh-ed25519 key'}]}))).toEqual({status: 'checked'});
  for (const body of [bytes({}), Buffer.from('\uFEFF{"ssh_keys":[]}'), bytes({ssh_keys: []})]) {
    await expect(registrationPreflight(opts, 'managed', ownership, null, env, () => body)).rejects.toThrow('SSH registration preflight failed');
  }
});
