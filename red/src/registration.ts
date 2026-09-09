import {get} from 'node:https';
import descriptors from '../resources/registration-preflight.json';

type Map = Record<string, any>;
type HTTP = (url: string, headers: Record<string, string>) => Promise<Uint8Array> | Uint8Array;
const missing = (v: any) => v == null || typeof v === 'string' && (!v.trim() || v.trim().toUpperCase() === 'REPLACE_ME');
const errors = new Set(['unowned SSH registration; verify whether hosts survive before explicit recovery',
  'foreign SSH registration; do not delete it; investigate or change profile']);

const nativeHTTP: HTTP = (url, headers) => new Promise((resolve, reject) => {
  let request: ReturnType<typeof get>;
  const timer = setTimeout(() => request.destroy(Error('request timeout')), 30000);
  request = get(url, {headers}, response => {
    if (response.statusCode !== 200) { response.destroy(); request.destroy(Error('request failed')); return; }
    const chunks: Buffer[] = [];
    let size = 0;
    response.on('data', chunk => {
      size += chunk.length;
      if (size > 2097152) request.destroy(Error('response too large'));
      else chunks.push(Buffer.from(chunk));
    });
    response.on('error', reject);
    response.on('end', () => { clearTimeout(timer); resolve(Buffer.concat(chunks)); });
  });
  request.on('error', error => { clearTimeout(timer); reject(error); });
});

function identifier(value: any): string {
  if (typeof value === 'string' && !missing(value)) return value;
  if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) return String(value);
  throw Error();
}
function material(value: any): string {
  if (typeof value !== 'string' || value.trim().split(/\s+/).length < 2) throw Error();
  return value.trim().split(/\s+/).slice(0, 2).join(' ');
}
const object = (value: any): value is Map => !!value && typeof value === 'object' && !Array.isArray(value);

export async function registrationPreflight(opts: Map, keyMode: string, ownership: Map | null = null,
  publicKey: string | null = null, environment: Record<string, string | undefined> | null = null, http: HTTP = nativeHTTP) {
  try {
    const provider = opts['provider-compute'];
    if (!['managed', 'external'].includes(keyMode) || typeof opts.profile !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$/.test(opts.profile)) throw Error();
    if (keyMode === 'external' || !Object.hasOwn(descriptors, provider) || opts['red/event'] === 'build' || opts['red/dry-run'] === true) return {status: 'skipped'};
    const descriptor = (descriptors as Map)[provider];
    let owned: string | null = null;
    if (ownership !== null) {
      if (!object(ownership) || Object.keys(ownership).sort().join(',') !== 'id,provider,scope' || ownership.provider !== provider || ownership.scope !== descriptor.scope) throw Error();
      owned = identifier(ownership.id);
    }
    const publicMaterial = publicKey === null ? null : material(publicKey);
    const token = (environment ?? process.env)[descriptor.token];
    if (typeof token !== 'string' || missing(token) || /[\r\n]/.test(token)) throw Error();
    const headers = {Authorization: 'Bearer ' + token, Accept: 'application/json'};
    const endpoint = new URL(descriptor.endpoint);
    const query = new URLSearchParams({per_page: String(descriptor.per_page)});
    if (descriptor.pagination === 'page') query.set('page', '1');
    let url = descriptor.endpoint + '?' + query;
    const visited = new Set<string>(), ids = new Set<string>();
    const matching: [string, string][] = [];
    let ownedSeen = false, ended = false;
    for (let page = 0; page < 1000; page++) {
      const parsed = new URL(url);
      const pairs = [...parsed.searchParams];
      const allowed = descriptor.pagination === 'cursor' ? ['per_page', 'cursor'] : ['per_page', 'page'];
      if (url.length > 4096 || parsed.protocol !== 'https:' || parsed.host !== endpoint.host || parsed.pathname !== endpoint.pathname || parsed.hash || parsed.username || parsed.password || new Set(pairs.map(([key]) => key)).size !== pairs.length || pairs.some(([key]) => !allowed.includes(key)) || visited.has(url)) throw Error();
      visited.add(url);
      const body = await http(url, {...headers});
      if (!(body instanceof Uint8Array) || body.length > 2097152) throw Error();
      const data = JSON.parse(new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(body));
      if (!object(data) || !Array.isArray(data.ssh_keys)) throw Error();
      for (const entry of data.ssh_keys) {
        if (!object(entry) || typeof entry.name !== 'string') throw Error();
        const id = identifier(entry.id), publicValue = material(entry[descriptor.public]);
        if (ids.has(id) || ids.size >= 100000) throw Error();
        ids.add(id);
        if (id === owned) ownedSeen = true;
        if (entry.name === opts.profile) matching.push([id, publicValue]);
      }
      const kind = descriptor.pagination;
      let next: any;
      if (kind === 'url') {
        const links = data.links ?? {};
        if (!object(links) || !object(links.pages ?? {})) throw Error();
        next = links.pages?.next;
      } else {
        const section = data.meta?.[kind === 'page' ? 'pagination' : 'links'];
        const field = kind === 'page' ? 'next_page' : 'next';
        if (!object(section) || !Object.hasOwn(section, field)) throw Error();
        next = section[field];
      }
      if (next == null || kind === 'cursor' && next === '') { ended = true; break; }
      if (kind === 'page') {
        if (typeof next !== 'number' || !Number.isSafeInteger(next) || next < 1) throw Error();
        url = descriptor.endpoint + '?' + new URLSearchParams({per_page: String(descriptor.per_page), page: String(next)});
      } else {
        if (typeof next !== 'string' || !next) throw Error();
        url = kind === 'url' ? next : descriptor.endpoint + '?' + new URLSearchParams({per_page: String(descriptor.per_page), cursor: next});
      }
    }
    if (!ended || owned !== null && !ownedSeen) throw Error();
    for (const [id, publicValue] of matching) {
      if (id !== owned) throw Error(publicMaterial !== null && publicMaterial === publicValue
        ? 'unowned SSH registration; verify whether hosts survive before explicit recovery'
        : 'foreign SSH registration; do not delete it; investigate or change profile');
      if (publicMaterial !== null && publicMaterial !== publicValue) throw Error();
    }
    return {status: 'checked'};
  } catch (error) {
    throw Error(error instanceof Error && errors.has(error.message) ? error.message : 'SSH registration preflight failed');
  }
}
