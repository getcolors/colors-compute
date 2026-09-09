"""Read-only registration collision checks. No imports, deletions or retries."""
import asyncio
import inspect
import json
import os
from importlib.resources import files
from urllib.parse import urlsplit, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .contract import _missing, _safe


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(url, headers):
    with build_opener(ProxyHandler({}), _NoRedirect()).open(Request(url, headers=headers), timeout=30) as response:
        if response.status != 200:
            raise ValueError()
        body = response.read(2097153)
        if len(body) > 2097152:
            raise ValueError()
        return body


async def _http(url, headers):
    return await asyncio.to_thread(_get, url, headers)


def _identifier(value):
    if isinstance(value, str) and not _missing(value):
        return value
    if type(value) in (int, float) and 0 < value <= 9007199254740991 and value == int(value):
        return str(int(value))
    raise ValueError()


def _public(value):
    if not isinstance(value, str) or len(value.split()) < 2:
        raise ValueError()
    return ' '.join(value.split()[:2])


async def registration_preflight(opts, key_mode, ownership=None, public_key=None, environment=None, http=None):
    """Return checked/skipped; failures raise only fixed, credential-free text."""
    try:
        descriptors = json.loads(files('colors_compute').joinpath('registration-preflight.json').read_text())
        provider = opts.get('provider-compute')
        if key_mode not in ('managed', 'external') or not _safe(opts.get('profile')):
            raise ValueError()
        if key_mode == 'external' or provider not in descriptors or opts.get('blue/event') == 'build' or opts.get('blue/dry-run') is True:
            return {'status': 'skipped'}
        descriptor = descriptors[provider]
        if ownership is not None:
            if not isinstance(ownership, dict) or set(ownership) != {'provider', 'scope', 'id'} or ownership['provider'] != provider or ownership['scope'] != descriptor['scope']:
                raise ValueError()
            owned_id = _identifier(ownership['id'])
        else:
            owned_id = None
        material = _public(public_key) if public_key is not None else None
        source = os.environ if environment is None else environment
        token = source.get(descriptor['token'])
        if not isinstance(token, str) or _missing(token) or '\n' in token or '\r' in token:
            raise ValueError()
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        endpoint = descriptor['endpoint']
        first = urlsplit(endpoint)
        query = {'per_page': descriptor['per_page']}
        if descriptor['pagination'] == 'page':
            query['page'] = 1
        url = endpoint + '?' + urlencode(query)
        visited, identifiers, matching, owned_seen = set(), set(), [], False
        for _ in range(1000):
            parsed = urlsplit(url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            allowed = {'per_page', 'page'} if descriptor['pagination'] == 'url' else {'per_page', 'page' if descriptor['pagination'] == 'page' else 'cursor'}
            if (len(url) > 4096 or parsed.scheme != 'https' or parsed.netloc != first.netloc or parsed.path != first.path or parsed.fragment
                    or parsed.username or parsed.password or len(dict(pairs)) != len(pairs) or set(dict(pairs)) - allowed or url in visited):
                raise ValueError()
            visited.add(url)
            body = (http or _http)(url, headers.copy())
            if inspect.isawaitable(body):
                body = await body
            if not isinstance(body, bytes) or len(body) > 2097152:
                raise ValueError()
            data = json.loads(body.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict) or not isinstance(data.get('ssh_keys'), list):
                raise ValueError()
            for entry in data['ssh_keys']:
                if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
                    raise ValueError()
                identity = _identifier(entry.get('id'))
                public = _public(entry.get(descriptor['public']))
                if identity in identifiers or len(identifiers) >= 100000:
                    raise ValueError()
                identifiers.add(identity)
                if identity == owned_id:
                    owned_seen = True
                if entry['name'] == opts['profile']:
                    matching.append((identity, public))
            kind = descriptor['pagination']
            if kind == 'url':
                links = data.get('links', {})
                if not isinstance(links, dict) or not isinstance(links.get('pages', {}), dict):
                    raise ValueError()
                next_value = links.get('pages', {}).get('next')
            else:
                meta = data.get('meta')
                section = meta.get('pagination' if kind == 'page' else 'links') if isinstance(meta, dict) else None
                field = 'next_page' if kind == 'page' else 'next'
                if not isinstance(section, dict) or field not in section:
                    raise ValueError()
                next_value = section[field]
            if next_value is None or kind == 'cursor' and next_value == '':
                break
            if kind == 'page':
                if type(next_value) is not int or next_value < 1:
                    raise ValueError()
                url = endpoint + '?' + urlencode({'per_page': descriptor['per_page'], 'page': next_value})
            else:
                if not isinstance(next_value, str) or not next_value:
                    raise ValueError()
                url = next_value if kind == 'url' else endpoint + '?' + urlencode({'per_page': descriptor['per_page'], 'cursor': next_value})
        else:
            raise ValueError()
        if owned_id is not None and not owned_seen:
            raise ValueError()
        for identity, public in matching:
            if identity != owned_id:
                if material is not None and material == public:
                    raise RuntimeError('unowned SSH registration; verify whether hosts survive before explicit recovery')
                raise RuntimeError('foreign SSH registration; do not delete it; investigate or change profile')
            if material is not None and material != public:
                raise ValueError()
        return {'status': 'checked'}
    except RuntimeError as error:
        if str(error) in ('unowned SSH registration; verify whether hosts survive before explicit recovery', 'foreign SSH registration; do not delete it; investigate or change profile'):
            raise ValueError(str(error)) from None
        raise ValueError('SSH registration preflight failed') from None
    except Exception:
        raise ValueError('SSH registration preflight failed') from None
