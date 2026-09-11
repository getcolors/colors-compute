import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {validate, credential_requirements, state_keys, expand, collect, state_decision, registry} from '../src/index';
test('packaged registry matches the canonical registry', () => {
  expect(registry).toEqual(JSON.parse(readFileSync(new URL('../../contracts/providers.json', import.meta.url), 'utf8')));
});
test('selection validation is ordered and secrets are backend independent', () => {
  expect(validate({})).toEqual([
    ':provider-compute must be one of aws, azure, digitalocean, google, hcloud, oci, vultr, yandex',
    ':provider-backend must be one of gcs, r2, s3', ':profile is required',
  ]);
  expect(credential_requirements({'provider-compute':'aws', 'provider-backend':'r2'})).toEqual(['COLORS_PAR_R2_ACCESS_KEY_ID', 'COLORS_PAR_R2_SECRET_ACCESS_KEY']);
  expect(credential_requirements({'provider-compute':'azure', 'provider-backend':'s3'})).toEqual([]);
  expect(() => credential_requirements({})).toThrow(':provider-compute must be one of');
});
test('selected required settings reject placeholders', () => {
  const opts: Record<string, unknown> = {'provider-compute':'vultr','provider-backend':'s3',profile:'demo'};
  for (const key of [...registry.compute.vultr.required, ...registry.backend.s3.required]) opts[key] = 'ok';
  expect(validate(opts)).toEqual([]);
  opts['vultr-plan'] = ' replace_me ';
  expect(validate(opts)).toEqual([':vultr-plan is required']);
});
test('scaling preserves node identities and state keys', () => {
  const one = expand([{role:'broker'}]);
  const many = expand([{role:'broker',count:3}]);
  expect(many[0]).toEqual(one[0]);
  expect(state_keys('demo', many.map(n => n.node_id)).nodes['broker-0']).toBe('demo/compute/nodes/broker-0.tfstate');
  expect(() => state_keys('../demo', ['0'])).toThrow(':profile must be a safe identifier');
  expect(() => state_keys('demo', ['0','0'])).toThrow('duplicate node_id: 0');
  expect(() => state_keys('demo', ['../0'])).toThrow('invalid node_id: ../0');
  expect(state_keys('demo', ['constructor']).nodes['constructor'] as unknown).toBe('demo/compute/nodes/constructor.tfstate');
});
test('malformed topologies are rejected', () => {
  for (const count of [false, 0, -1, 1.5, '2', null]) expect(() => expand([{count}])).toThrow('count must be a positive integer');
  expect(() => expand([])).toThrow('topology must declare at least one role');
  expect(() => expand([{role:'Bad'}])).toThrow('invalid role');
  expect(() => expand([{role:'a'}, {role:'a'}])).toThrow('duplicate role');
  expect(() => expand([{}, {role:'a'}])).toThrow('a null role must be the only role');
});
const requests = expand([{role:'broker', count:2}]);
const results = requests.map(r => ({...r,provider:'vultr',name:r.node_id,ip:'192.0.2.1',user:'root',sudoer:'root',metadata:{uid:r.node_id}}));
test('join orders by requests and preserves metadata without mutating results', () => {
  const reversed = [...results].reverse().map(r => ({...r, role:'untrusted'}));
  const joined = collect(requests, reversed, 'broker-0');
  expect(joined.nodes).toEqual(results);
  expect(reversed[0].role).toBe('untrusted');
});
test('join refuses missing, duplicate, undeclared, incomplete and wrong-provider nodes', () => {
  expect(() => collect(requests, results.slice(0,1), 'broker-0')).toThrow('missing node: broker-1');
  expect(() => collect(requests, [...results, results[0]], 'broker-0')).toThrow('duplicate node: broker-0');
  expect(() => collect(requests, [{node_id:'other'}], 'broker-0')).toThrow('undeclared node: other');
  expect(() => collect(requests, [{...results[0],user:' '},results[1]], 'broker-0')).toThrow('incomplete node broker-0: user');
  expect(() => collect(requests, [results[0], {...results[1],provider:'aws'}], 'broker-0')).toThrow('provider mismatch: broker-1');
  expect(() => collect(requests.map(r => ({...r,private:true})), results, 'broker-0')).toThrow('incomplete node broker-0: vpc_ip');
});
test('unreadable and legacy states never authorize mutation', () => {
  expect(state_decision({status:'absent'},'aws')).toEqual({action:'create'});
  expect(state_decision({status:'present',params:{provider:'aws'}},'aws')).toEqual({action:'reuse'});
  expect(() => state_decision({status:'error'},'aws')).toThrow('could not read compute state; refusing mutation');
  expect(() => state_decision({status:'present'},'aws')).toThrow('legacy state requires migration');
  expect(() => state_decision({status:'present',params:{provider:'vultr'}},'aws')).toThrow('state holds a vultr machine; set provider-compute back to vultr and delete first');
});
test('registry is deeply frozen and prototype names cannot select a provider', () => {
  expect(Object.isFrozen(registry.compute.vultr.required)).toBe(true);
  expect(validate({'provider-compute':'__proto__','provider-backend':'constructor',profile:'demo'})).toHaveLength(2);
});
test('join rejects mixed provider requests and normalizes omitted index', () => {
  expect(() => collect([{node_id:'0',provider:'aws'},{node_id:'1',provider:'vultr'}],
    [{...results[0],node_id:'0',provider:'aws'},{...results[1],node_id:'1'}], '0')).toThrow('provider mismatch: 1');
  expect(collect([{node_id:'0'}], [{...results[0],node_id:'0'}], '0').nodes[0].index).toBeNull();
});
test('identifiers and roles reject trailing line terminators',()=>{
  for (const suffix of ['\n','\r','\r\n','\u2028','\u2029']) {
    expect(()=>state_keys('demo'+suffix,['0'])).toThrow(':profile must be a safe identifier');
    expect(()=>state_keys('demo',['0'+suffix])).toThrow('invalid node_id:');
    expect(()=>expand([{role:'broker'+suffix}])).toThrow('invalid role');
  }
});
