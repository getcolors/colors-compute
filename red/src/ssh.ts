/** Named encrypted SSH resources, with a dedicated agent per caller scope. */
import {mkdtempSync, readFileSync, writeFileSync, chmodSync, rmSync} from 'node:fs';
import {isAbsolute, resolve, join} from 'node:path';
import {tmpdir} from 'node:os';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
type Map = Record<string, any>;
type Env = Record<string, string | undefined>;
function need(ok: unknown, message: string): asserts ok { if (!ok) throw Error(message); }
const match = (re: RegExp, v: unknown): v is string => typeof v === 'string' && re.exec(v)?.[0] === v;
function canonical(v: any): string {
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
  return JSON.stringify(v);
}
export function ssh_plan(opts: Map, request: Map) {
  need(request && Object.keys(request).every(k => ['name','workdir','passphrase_env','backend','expected','new_passphrase_env','allow_delete','consumers_destroyed','lock_token'].includes(k)), 'invalid SSH request');
  need(match(/^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/, opts.profile) && match(/^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/, request.name)
    && typeof request.workdir === 'string' && isAbsolute(request.workdir) && resolve(request.workdir) === request.workdir && !/[\\\0]/.test(request.workdir), 'invalid SSH resource identity');
  need(match(/^COLORS_PAR_[A-Z][A-Z0-9_]*$/, request.passphrase_env), 'invalid passphrase binding');
  const override = request.backend ?? {};
  need(override && typeof override === 'object' && !Array.isArray(override) && Object.keys(override).every(k => ['provider-backend','s3-prefix','s3-bucket','s3-region','r2-bucket','r2-endpoint','oci-bucket','oci-region','oci-namespace','gcs-bucket'].includes(k)), 'invalid SSH backend override');
  const settings = {...opts, ...override}, kind = settings['provider-backend'], prefix = settings['s3-prefix'] ?? '';
  need(['local','s3','r2','oci','gcs'].includes(kind), 'invalid SSH backend');
  need(typeof prefix === 'string' && (!prefix || prefix.split('/').every((p: string) => match(/^[A-Za-z0-9][A-Za-z0-9_.-]*$/, p))), 'invalid SSH prefix');
  const directory = join(request.workdir, opts.profile, 'ssh', request.name);
  const object_key = [prefix, opts.profile, 'ssh', request.name, 'resource.json'].filter(Boolean).join('/');
  const storage: Map = {kind};
  if (kind === 'local') storage.path = directory + '/resource.json';
  else {
    const bucket = settings[kind + '-bucket'];
    need(match(/^[A-Za-z0-9][A-Za-z0-9._-]{1,221}$/, bucket), 'invalid SSH bucket');
    storage.bucket = bucket;
    if (kind !== 'gcs') {
      const region = kind === 'r2' ? 'auto' : settings[kind + '-region'];
      need(match(/^[A-Za-z0-9_-]+$/, region), 'invalid SSH region'); storage.region = region;
    }
    if (['r2','oci'].includes(kind)) {
      if (kind === 'oci') need(match(/^[A-Za-z0-9][A-Za-z0-9_-]*$/, settings['oci-namespace']), 'invalid OCI namespace');
      const endpoint = kind === 'r2' ? settings['r2-endpoint'] : `https://${settings['oci-namespace'] ?? ''}.compat.objectstorage.${storage.region}.oraclecloud.com`;
      need(match(/^https:\/\/[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?\/?$/, endpoint), 'invalid SSH endpoint');
      const url = new URL(endpoint); need(!!url.hostname && url.hostname.split('.').every(label => match(/^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/, label)) && !url.username && !url.password && (!url.port || Number(url.port) >= 1 && Number(url.port) <= 65535), 'invalid SSH endpoint');
      storage.endpoint = endpoint.replace(/\/$/, ''); storage.credential_prefix = kind.toUpperCase();
    }
  }
  const identity: Map = {version: 2, profile: opts.profile, name: request.name, storage};
  if (kind !== 'local') identity.object_key = object_key;
  return {status: 'planned', directory, object_key, storage, reference: canonical(identity)};
}
const baseEnv = ['PATH','HOME','TMPDIR','SYSTEMROOT','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN',
  'AWS_PROFILE','AWS_DEFAULT_PROFILE','AWS_CONFIG_FILE','AWS_SHARED_CREDENTIALS_FILE','AWS_CA_BUNDLE',
  'GOOGLE_APPLICATION_CREDENTIALS','CLOUDSDK_CONFIG','COLORS_PAR_R2_ACCESS_KEY_ID','COLORS_PAR_R2_SECRET_ACCESS_KEY',
  'COLORS_PAR_OCI_ACCESS_KEY_ID','COLORS_PAR_OCI_SECRET_ACCESS_KEY'];
function environment(source: Env, requests: Map[]) {
  const allowed = new Set([...baseEnv, ...requests.flatMap(r => [r.passphrase_env, r.new_passphrase_env]).filter(Boolean)]);
  return Object.fromEntries(Object.entries(source).filter(([k,v]) => allowed.has(k) && typeof v === 'string')) as Record<string,string>;
}
function start(message: Map, env: Record<string,string>) {
  const directory = mkdtempSync(join(tmpdir(), 'colors-ssh-runtime-'));
  const path = join(directory, 'ssh-adapter');
  writeFileSync(path, readFileSync(new URL('../resources/ssh_adapter.py', import.meta.url)), {mode: 0o700});
  chmodSync(directory, 0o700);
  const child = spawn('python3', [path], {env, stdio: ['pipe','pipe','pipe']});
  child.stderr.resume();
  let exited = false;
  const done = new Promise<void>(resolve => { child.once('exit', () => {exited = true; resolve();}); child.once('error', () => {exited = true; resolve();}); });
  let closing: Promise<void> | undefined;
  const close = () => closing ??= (async () => {
    const wait = async (ms: number) => { let timer: ReturnType<typeof setTimeout> | undefined; await Promise.race([done, new Promise(r => {timer = setTimeout(r, ms);})]); if(timer) clearTimeout(timer); };
    try {
      child.stdin.end();
      await wait(10000);
      if (!exited) {child.kill('SIGTERM'); await wait(5000);}
      if (!exited) {child.kill('SIGKILL'); await wait(5000);}
      need(exited, 'SSH adapter did not terminate');
    } finally {
      child.stdin.destroy(); child.stdout.destroy(); child.stderr.destroy();
      rmSync(directory, {recursive: true, force: true});
    }
  })();
  const ready = new Promise<Map>((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('SSH resource operation timed out')), 180000);
    let buffer = '';
    const fail = () => {clearTimeout(timer); reject(Error('SSH resource operation failed'));};
    child.once('error', fail); child.once('close', fail);
    child.stdout.on('data', data => {
      buffer += data.toString();
      if (buffer.includes('\n')) {
        clearTimeout(timer); child.removeListener('close', fail);
        try {resolve(JSON.parse(buffer.split('\n')[0]!));} catch {reject(Error('invalid SSH adapter result'));}
      }
    });
  });
  // Registration can fail before the caller starts awaiting readiness.
  void ready.catch(() => {});
  child.stdin.on('error', () => {});
  child.stdin.write(JSON.stringify(message) + '\n');
  return {ready, close};
}
export async function ssh_resource(opts: Map, request: Map, operation = 'create', source: Env = process.env) {
  const plan = ssh_plan(opts, request);
  if (operation === 'build') return {...plan, status:'built'};
  const handle = start({plan, request, operation}, environment(source, [request]));
  try {return await handle.ready;} finally {await handle.close();}
}
export async function start_agent(resources: Map[], source: Env, register: (phase: 'resource', cleanup: () => Promise<void>) => unknown, lifetime = 900) {
  const entries: Map[] = resources.map(e => ({plan: ssh_plan(e.opts, e.request), request: e.request, resource: e.resource}));
  const handle = start({operation:'agent', resources:entries, lifetime}, environment(source, entries.map(e => e.request)));
  try {
    register('resource', handle.close);
    const result = await handle.ready;
    need(result.status === 'ready', 'SSH agent setup failed');
    return result;
  } catch (error) {await handle.close(); throw error;}
}
