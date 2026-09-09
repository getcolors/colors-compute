import {expect,test} from 'bun:test';
import {existsSync,readFileSync,statSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {journalGet,journalPut} from '../src/journal.ts';
import {coordination} from '../src/coordination.ts';
const opts={'profile':'demo','provider-compute':'vultr','provider-backend':'r2','r2-bucket':'states','r2-endpoint':'https://example.invalid'};
const identity={profile:'demo',provider:'vultr',backend:{kind:'r2',bucket:'states',region:'auto',endpoint:'https://example.invalid'}};
const intent=coordination({status:'absent'},identity,{type:'acquire',run_id:'run-1',write_id:'write-1',target_etag:null});
const environment={COLORS_PAR_R2_ACCESS_KEY_ID:'SYNTHETIC_ACCESS',COLORS_PAR_R2_SECRET_ACCESS_KEY:'SYNTHETIC_SECRET',AWS_ACCESS_KEY_ID:'AMBIENT_ACCESS',AWS_SECRET_ACCESS_KEY:'AMBIENT_SECRET',AWS_SESSION_TOKEN:'ambient-session',AWS_PROFILE:'ambient-profile',AWS_CONFIG_FILE:'/ambient-config',AWS_CA_BUNDLE:'/trusted-ca',AWS_ENDPOINT_URL:'https://unrelated.invalid',COLORS_PAR_OTHER:'other',PATH:process.env.PATH};
const success={exit:0,out:'{"ETag":"opaque-etag"}',err:''};
function bodyPath(args:string[]) {return args[2]==='get-object'?args[7]:args[args.indexOf('--body')+1];}
test('R2 get isolates INI credentials and returns bounded untrusted document',async()=>{
  let directory='';
  const result=await journalGet(opts,environment,async(args,options)=>{
    directory=options.cwd;
    expect(args.slice(0,7)).toEqual(['aws','s3api','get-object','--bucket','states','--key','demo/compute/coordination.json']);
    expect(statSync(directory).mode&0o777).toBe(0o700);
    for(const name of ['body.json','credentials','config'])expect(statSync(join(directory,name)).mode&0o777).toBe(0o600);
    expect(readFileSync(join(directory,'credentials'),'utf8')).toBe('[default]\naws_access_key_id = SYNTHETIC_ACCESS\naws_secret_access_key = SYNTHETIC_SECRET\n');
    expect(readFileSync(join(directory,'config'),'utf8')).toBe('');
    expect(options.env.AWS_CA_BUNDLE).toBe('/trusted-ca');
    for(const key of ['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_PROFILE','AWS_ENDPOINT_URL','COLORS_PAR_OTHER'])expect(options.env[key]).toBeUndefined();
    expect(options.env.AWS_MAX_ATTEMPTS).toBe('1');expect(options.env.AWS_PAGER).toBe('');expect(options.env.AWS_CLI_AUTO_PROMPT).toBe('off');
    expect(options.env.AWS_REQUEST_CHECKSUM_CALCULATION).toBe('when_required');expect(options.timeoutMs).toBe(120000);
    expect(JSON.stringify(args)).not.toContain('SYNTHETIC');
    writeFileSync(bodyPath(args),JSON.stringify({untrusted:'document'}));return success;
  });
  expect(result).toEqual({status:'present',etag:'opaque-etag',document:{untrusted:'document'}});
  expect(existsSync(directory)).toBe(false);expect(environment.AWS_PROFILE).toBe('ambient-profile');
});
test('S3 preserves ambient credential chain without writing credentials',async()=>{
  expect(await journalGet({profile:'demo','provider-backend':'s3','s3-bucket':'states','s3-region':'eu-west-1'},environment,async(args,options)=>{
    expect(options.env.AWS_PROFILE).toBe('ambient-profile');expect(options.env.AWS_SESSION_TOKEN).toBe('ambient-session');
    expect(existsSync(join(options.cwd,'credentials'))).toBe(false);expect(options.env.COLORS_PAR_R2_ACCESS_KEY_ID).toBeUndefined();
    expect(args).not.toContain('--endpoint-url');writeFileSync(bodyPath(args),'{}');return success;
  })).toEqual({status:'present',etag:'opaque-etag',document:{}});
});
test('get distinguishes exact NoSuchKey service prefix from all other errors',async()=>{
  for(const [err,status] of [
    ['An error occurred (NoSuchKey) when calling the GetObject operation: missing','absent'],
    [' \nAn error occurred (NoSuchKey) when calling the GetObject operation: missing','absent'],
    ['An error occurred (NoSuchBucket) when calling the GetObject operation: missing','error'],
    ['An error occurred (AccessDenied) when calling the GetObject operation: NoSuchKey','error'],
    ['message\nAn error occurred (NoSuchKey) when calling the GetObject operation: missing','error'],
    ['An error occurred (NoSuchKey) when calling the PutObject operation: missing','error'],
    ['404 Not Found','error'],
  ] as const)expect((await journalGet(opts,environment,async()=>({exit:255,out:'NoSuchKey',err}))).status).toBe(status);
});
test('get rejects oversized bodies, missing etags and malformed bodies and cleans up',async()=>{
  for(const [body,out] of [['{}','{}'],['','{"ETag":"tag"}'],['[]','{"ETag":"tag"}'],['invalid','{"ETag":"tag"}'],[' '.repeat(2*1024*1024)+'{}','{"ETag":"tag"}'],['{"value":"SYNTHETIC_SECRET"}','{"ETag":"tag"}']]) {
    let directory='';
    expect(await journalGet(opts,environment,async(args,options)=>{directory=options.cwd;writeFileSync(bodyPath(args),body);return {...success,out};})).toEqual({status:'error'});
    expect(existsSync(directory)).toBe(false);
  }
});
test('put performs one conditional creation and matching update with private body',async()=>{
  for(const condition of [{if_none_match:'*'},{if_match:'old-etag'}]) {
    let calls=0;let directory='';
    const result=await journalPut(opts,{...intent,condition},environment,async(args,options)=>{
      calls++;directory=options.cwd;
      expect(statSync(bodyPath(args)).mode&0o777).toBe(0o600);
      expect(JSON.parse(readFileSync(bodyPath(args),'utf8'))).toEqual(intent.document);
      expect(args).toContain('if_match' in condition?'--if-match':'--if-none-match');
      expect(args).toContain('if_match' in condition?'old-etag':'*');
      expect(JSON.stringify(args)).not.toContain('SYNTHETIC');return success;
    });
    expect(result).toEqual({status:'written',etag:'opaque-etag'});expect(calls).toBe(1);expect(existsSync(directory)).toBe(false);
  }
});
test('put rejects invalid intent, extensions and mismatched identity before subprocess',async()=>{
  let calls=0;
  for(const bad of [null,{}, {...intent,extra:'secret'}, {...intent,condition:{}}, {...intent,condition:{if_match:' ',if_none_match:'*'}}, {...intent,document:{...intent.document,metadata:'secret'}}, {...intent,document:{...intent.document,identity:{...identity,profile:'other'}}}]) {
    expect(await journalPut(opts,bad,environment,async()=>{calls++;return success;})).toEqual({status:'error'});
  }
  expect(calls).toBe(0);
});
test('conditional service conflicts differ from ambiguous failures without retry',async()=>{
  for(const [err,status] of [
    ['An error occurred (PreconditionFailed) when calling the PutObject operation: conflict','conflict'],
    ['An error occurred (ConditionalRequestConflict) when calling the PutObject operation: conflict','conflict'],
    ['An error occurred (PreconditionFailed) when calling the GetObject operation: conflict','error'],
    ['An error occurred (AccessDenied) when calling the PutObject operation: PreconditionFailed','error'],
    ['earlier text\nAn error occurred (PreconditionFailed) when calling the PutObject operation: conflict','error'],
    ['timeout','error'],
  ] as const) {
    let calls=0;expect((await journalPut(opts,intent,environment,async()=>{calls++;return {exit:1,out:'',err};})).status).toBe(status);expect(calls).toBe(1);
  }
});
test('invalid R2 credentials and thrown execution errors never leak',async()=>{
  let calls=0;
  for(const secret of ['', ' REPLACE_ME ', 'line\nbreak', 'line\rbreak'])expect(await journalGet(opts,{...environment,COLORS_PAR_R2_SECRET_ACCESS_KEY:secret},async()=>{calls++;return success;})).toEqual({status:'error'});
  expect(calls).toBe(0);let directory='';
  expect(await journalPut(opts,intent,environment,async(args,options)=>{directory=options.cwd;throw new Error('SYNTHETIC_SECRET');})).toEqual({status:'error'});expect(existsSync(directory)).toBe(false);
});
test('explicit runner cancellation propagates after private session cleanup',async()=>{
  let directory='';
  await expect(journalGet(opts,environment,async(args,options)=>{directory=options.cwd;throw new DOMException('cancelled','AbortError');})).rejects.toThrow('cancelled');
  expect(existsSync(directory)).toBe(false);
});
test('modern AWS CLI service errors support exact prefix and retry suffix only',async()=>{
  expect(await journalGet(opts,environment,async()=>({exit:254,out:'',err:'\naws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (reached max retries: 0): synthetic response\n'}))).toEqual({status:'absent'});
  expect(await journalPut(opts,intent,environment,async()=>({exit:254,out:'',err:'aws: [ERROR]: An error occurred (PreconditionFailed) when calling the PutObject operation (reached max retries: 0): synthetic response'}))).toEqual({status:'conflict'});
  for(const err of [
    'aws: [ERROR]: An error occurred (NoSuchKey) when calling the PutObject operation (reached max retries: 0): response',
    'untrusted: aws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (reached max retries: 0): response',
    'aws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (unrecognized suffix): response',
    'aws: [ERROR]: An error occurred (AccessDenied) when calling the GetObject operation (reached max retries: 0): NoSuchKey',
  ]) expect(await journalGet(opts,environment,async()=>({exit:254,out:'',err}))).toEqual({status:'error'});
});
test('journal bodies require strict UTF-8 and reject a BOM',async()=>{
  const bodies=[
    Buffer.from([0x7b,0x22,0x78,0x22,0x3a,0x22,0xff,0x22,0x7d]),
    Buffer.from([0xff,0xfe,0x7b,0x00,0x7d,0x00]),
    Buffer.from([0xef,0xbb,0xbf,0x7b,0x7d]),
  ];
  expect(new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bodies[2]).charCodeAt(0)).toBe(0xfeff);
  for(const body of bodies) {
    let directory='';
    expect(await journalGet(opts,environment,async(args,options)=>{directory=options.cwd;writeFileSync(bodyPath(args),body);return success;})).toEqual({status:'error'});
    expect(existsSync(directory)).toBe(false);
  }
});
