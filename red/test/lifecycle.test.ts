import {expect,test} from 'bun:test';
import fixtures from '../../test/fixtures/lifecycle.json';
import {coordination,documentValid} from '../src/coordination.ts';
import {lifecycleRepair} from '../src/lifecycle.ts';
for(const fixture of fixtures as any[])test(fixture.name,()=>{
  const args=structuredClone(fixture.args);const before=JSON.stringify(args);
  const fn:(...a:any[])=>any=fixture.op==='lifecycle_repair'?lifecycleRepair:coordination;
  if(fixture.expected.error)expect(()=>fn(...args)).toThrow(fixture.expected.error);
  else {const result=fn(...args);expect(result).toEqual(fixture.expected);expect(documentValid(result.document)).toBe(true);}
  expect(JSON.stringify(args)).toBe(before);
});
