import {expect,test} from 'bun:test';
import fixtures from '../../test/fixtures/coordination.json';
import {coordination} from '../src/index.ts';
for(const fixture of fixtures) {
  test(fixture.name,()=>{
    const input=structuredClone(fixture.args);
    const before=JSON.stringify(input);
    if('error' in fixture.expected) expect(()=>coordination(input[0],input[1],input[2])).toThrow(fixture.expected.error);
    else expect(coordination(input[0],input[1],input[2])).toEqual(fixture.expected);
    expect(JSON.stringify(input)).toBe(before);
  });
}
test('plans are detached from observed document and expected identity',()=>{
  const fixture=fixtures.find(item=>item.name === 'declare complete intended node set')!;
  const input=structuredClone(fixture.args);
  const result=coordination(input[0],input[1],input[2]);
  result.document.identity.profile='changed';
  expect((input[0] as any).document.identity.profile).toBe('demo');
  expect((input[1] as any).profile).toBe('demo');
});
