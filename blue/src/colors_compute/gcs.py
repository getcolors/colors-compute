"""GCS JSON API transport using the active gcloud account."""
import asyncio
import json
import os
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .backend import _run


async def gcs_client(environment=None, runner=None):
    env = {k: v for k, v in dict(os.environ if environment is None else environment).items()
           if isinstance(v, str) and not k.startswith(('COLORS_PAR_', 'TF_', 'TOFU_'))}
    token = await (runner or _run)(['gcloud', 'auth', 'print-access-token', '--quiet'], os.getcwd(), env, 120000)
    if token.exit or not token.out.strip():
        raise ValueError('GCS authentication failed')

    async def request(method, path, body=None, query=None):
        url = 'https://storage.googleapis.com/' + path
        if query:
            url += '?' + urlencode({k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in query.items()})
        headers = {'Authorization': 'Bearer ' + token.out.strip()}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body, allow_nan=False).encode()

        def send():
            try:
                with urlopen(Request(url, data=data, headers=headers, method=method), timeout=120) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else {}
            except HTTPError as error:
                if error.code == 404:
                    return None
                if error.code == 412:
                    return {'conflict': True}
                raise ValueError(f'GCS operation failed ({error.code})') from None
        return await asyncio.to_thread(send)
    return request


def bucket_path(bucket):
    return 'storage/v1/b/' + quote(bucket, safe='')


def object_path(bucket, key):
    return bucket_path(bucket) + '/o/' + quote(key, safe='')


async def gcs_get(request, bucket, key):
    metadata = await request('GET', object_path(bucket, key))
    if metadata is None:
        return None
    document = await request('GET', object_path(bucket, key), query={'alt': 'media', 'generation': metadata['generation']})
    if document is None:
        raise ValueError('GCS generation disappeared')
    return {'document': document, 'etag': metadata['generation']}


async def gcs_put(request, bucket, key, document, generation):
    return await request('POST', 'upload/storage/v1/b/' + quote(bucket, safe='') + '/o', document,
                         {'uploadType': 'media', 'name': key, 'ifGenerationMatch': generation})
