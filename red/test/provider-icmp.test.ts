import {expect,test} from 'bun:test';
import fixtures from '../../test/fixtures/provider-icmp.json';
import {provider_request} from '../src/provider-request.ts';
for(const fixture of fixtures as any[])test(fixture.name,()=>{
 const [opts,stage,request,shared]=fixture.args;
 if(fixture.expected.error)expect(()=>provider_request(opts,stage,request,shared)).toThrow(fixture.expected.error);
 else expect(provider_request(opts,stage,request,shared)).toEqual(fixture.expected);
});
