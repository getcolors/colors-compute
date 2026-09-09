import {expect,test} from 'bun:test';
import fixtures from '../../test/fixtures/provider-endpoint.json';
import {provider_request} from '../src/provider-request.ts';
import {endpoint_agent} from '../src/endpoint.ts';
for(const fixture of fixtures as any[])test(fixture.name,()=>{
 const [opts,stage,request,shared]=fixture.args;
 if(fixture.expected.error)expect(()=>provider_request(opts,stage,request,shared)).toThrow(fixture.expected.error);
 else expect(provider_request(opts,stage,request,shared)).toEqual(fixture.expected);
});
test('standalone artifact is packaged and credentials descriptor detached',()=>{
 const first=endpoint_agent('digitalocean');expect(first.content).toContain('def operate(');first.credentials.push('changed');expect(endpoint_agent('digitalocean').credentials).toEqual(['COLORS_PAR_DO_TOKEN']);expect(()=>endpoint_agent('aws')).toThrow('unsupported');
});
