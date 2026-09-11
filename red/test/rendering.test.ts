import {expect, test} from 'bun:test';
import {render_template, backend_plan} from '../src/index.ts';
test('render preserves JSON types, expressions and keys and isolates inputs', () => {
  const inputs = {count:3, enabled:false, absent:null, nested:{items:['a']}};
  const template = {'{{key}}':['{{count}}','{{enabled}}','{{absent}}','{{nested}}'], expr:'${resource.node.id}'};
  const result = render_template(template, inputs) as any;
  expect(result).toEqual({'{{key}}':[3,false,null,{items:['a']}],expr:'${resource.node.id}'});
  result['{{key}}'][3].items.push('b');
  expect(inputs.nested.items).toEqual(['a']);
  expect(template['{{key}}'][0]).toBe('{{count}}');
});
test('render rejects partial, malformed and missing substitutions', () => {
  for (const value of ['prefix {{name}}','{{Name}}','{{name1}}','{{ name }}','oops }}','{{']) {
    expect(() => render_template(value, {})).toThrow('template placeholders must occupy the entire string');
  }
  expect(() => render_template('{{name}}', {})).toThrow('missing template input: name');
  expect(() => render_template('{{constructor}}', {})).toThrow('missing template input: constructor');
});
const s3 = {'provider-backend':'s3', 's3-bucket':'states', 's3-region':'eu-west-1'};
const r2 = {'provider-backend':'r2', 'r2-bucket':'states', 'r2-endpoint':'https://example.invalid'};
test('S3 retains ambient AWS credentials without exposing any secret values', () => {
  const env = {...process.env};
  const plan = backend_plan({...s3, AWS_ACCESS_KEY_ID:'sentinel-secret'}, 'demo/compute/nodes/0.tfstate');
  expect(plan).toEqual({config:{terraform:{backend:{s3:{bucket:'states',region:'eu-west-1',key:'demo/compute/nodes/0.tfstate',use_lockfile:true}}}},credential_bindings:{},environment:{}});
  expect(JSON.stringify(plan)).not.toContain('sentinel-secret');
  expect(process.env).toEqual(env);
});
test('R2 plans protected backend credential bindings without AWS environment substitution', () => {
  const env = {...process.env};
  const plan = backend_plan({...r2, 'r2-secret-access-key':'sentinel-secret'}, 'demo/shared.tfstate');
  expect(plan.credential_bindings).toEqual({COLORS_PAR_R2_ACCESS_KEY_ID:'access_key',COLORS_PAR_R2_SECRET_ACCESS_KEY:'secret_key'});
  expect(plan.environment).toEqual({});
  expect(plan.config.terraform.backend.s3).toEqual({bucket:'states',region:'auto',key:'demo/shared.tfstate',use_lockfile:true,endpoints:{s3:'https://example.invalid'},use_path_style:false,skip_credentials_validation:true,skip_metadata_api_check:true,skip_region_validation:true,skip_requesting_account_id:true,skip_s3_checksum:true});
  expect(JSON.stringify(plan)).not.toContain('sentinel-secret');
  expect(process.env).toEqual(env);
});
test('backend validates selection then sorted requirements then state key', () => {
  expect(() => backend_plan({}, '')).toThrow(':provider-backend must be one of gcs, r2, s3');
  expect(() => backend_plan({'provider-backend':'s3'}, '')).toThrow(':s3-bucket is required');
  expect(() => backend_plan({...s3,'s3-region':' replace_me '}, '')).toThrow(':s3-region is required');
  for (const key of ['', '../state', 'a/../b', './a', 'a//b', '/a', 'a/', 'a b', 'a\\b']) {
    expect(() => backend_plan(s3, key)).toThrow('invalid state key');
  }
  expect(backend_plan(s3, 'a/b_0.tfstate').config.terraform.backend.s3.key).toBe('a/b_0.tfstate');
});
test('state paths and whole placeholders reject trailing line terminators',()=>{
  for (const suffix of ['\n','\r','\r\n','\u2028','\u2029']) {
    expect(()=>backend_plan(s3,'demo/state'+suffix)).toThrow('invalid state key');
    expect(()=>render_template('{{name}}'+suffix,{name:'example'})).toThrow('template placeholders must occupy the entire string');
  }
});
