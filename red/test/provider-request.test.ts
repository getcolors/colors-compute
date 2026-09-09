import {expect,test} from 'bun:test';
import fixtures from '../../test/fixtures/provider-requests.json';
import {provider_request} from '../src/provider-request.ts';
for(const fixture of fixtures as any[]) {
  test(fixture.name,()=>{
    const input=structuredClone(fixture.args) as any[];const before=JSON.stringify(input);
    if('error' in fixture.expected)expect(()=>provider_request(input[0],input[1],input[2],input[3])).toThrow(fixture.expected.error);
    else expect(provider_request(input[0],input[1],input[2],input[3])).toEqual(fixture.expected);
    expect(JSON.stringify(input)).toBe(before);
  });
}
function sample(name='vultr-shared'):any[]{return structuredClone((fixtures as any[]).find(item=>item.name===name)!.args);}
function execute(args:any[]){return provider_request(args[0],args[1],args[2],args[3]);}
test('rendered values and returned documents cannot mutate requests or recipes',()=>{
  const args=sample();const before=JSON.stringify(args);const first=execute(args);const expected=structuredClone(first);
  first.inputs.ingress={mutated:true};first.documents={};
  expect(JSON.stringify(args)).toBe(before);expect(execute(args)).toEqual(expected);
});
test('Google rule resource names remain stable when adding earlier-sorted ingress',()=>{
  const args=sample('google-shared');const previous=execute(args).inputs.ingress;
  args[2].security.ingress.push({id:'aaa',protocol:'udp',from_port:53,to_port:53,sources:['192.0.2.2/32']});
  const updated=execute(args).inputs.ingress;
  for(const key of Object.keys(previous))expect(updated[key]).toEqual(previous[key]);
});
test('request capabilities, ingress, and ownership are validated before rendering',()=>{
  const cases:[(args:any[])=>void,string][]=[
    [args=>{args[2].secret='do-not-echo';},'invalid compute request'],
    [args=>{args[2].network.mode='none';},'unsupported compute network mode'],
    [args=>{args[2].security.ingress[0].sources.push(args[2].security.ingress[0].sources[0]);},'invalid compute ingress'],
    [args=>{args[2].network.cidr='10.42.0.1/16';},'invalid compute network CIDR'],
    [args=>{args[2].security.egress='restricted';},'unsupported compute security policy'],
    [args=>{args[2].security.ingress[0].from_port=true;},'invalid compute ingress'],
    [args=>{args[2].network.private_ip=175112193;},'unsupported compute static private address'],
  ];
  for(const [mutate,message] of cases){const args=sample();mutate(args);expect(()=>execute(args)).toThrow(message);}
  const args=sample('vultr-node');args[3].params.provider='aws';expect(()=>execute(args)).toThrow('compute shared provider mismatch');
});
test('request literals cannot inject Terraform expressions through options or shared references',()=>{
  for(const token of ['${file("/private")}', '%{if true}']){
    const args=sample('vultr-node');args[0]['vultr-plan']=token;expect(()=>execute(args)).toThrow('invalid compute literal');
    const shared=sample('vultr-node');shared[3].params.vpc_id=token;expect(()=>execute(shared)).toThrow('invalid compute literal');
    const key=sample('vultr-shared');key[2].key.public_key=token;expect(()=>execute(key)).toThrow('invalid compute literal');
  }
});
test('external key references reject null and nonpositive IDs',()=>{
  for(const ids of [null,[0],[-1],[true],[1.5]]){
    const args=sample('vultr-node');args[2].key={mode:'external',ids};expect(()=>execute(args)).toThrow('invalid compute key references');
  }
  const args=sample('vultr-node');args[2].key={mode:'external',ids:[1]};expect(execute(args).inputs.ssh_key_ids).toEqual([1]);
});
