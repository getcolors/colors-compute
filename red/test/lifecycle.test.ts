import {expect,test} from 'bun:test';
import fixtures from '../../test/fixtures/lifecycle.json';
import {coordination,documentValid} from '../src/coordination.ts';
for(const fixture of fixtures as any[])test(fixture.name,()=>{
  const args=structuredClone(fixture.args);const before=JSON.stringify(args);
  if(fixture.expected.error)expect(()=>coordination(...args as [unknown,unknown,unknown])).toThrow(fixture.expected.error);
  else {const result=coordination(...args as [unknown,unknown,unknown]);expect(result).toEqual(fixture.expected);expect(documentValid(result.document)).toBe(true);}
  expect(JSON.stringify(args)).toBe(before);
});
