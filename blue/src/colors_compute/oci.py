"""OCI Object Storage requests signed by the operator's OCI CLI profile."""
import json
import os
from urllib.parse import quote, urlencode
from .backend import _run


def bucket_path(opts):
    return '/n/' + quote(opts['oci-namespace'], safe='') + '/b/' + quote(opts['oci-bucket'], safe='')


def object_path(opts, key):
    return bucket_path(opts) + '/o/' + quote(key, safe='')


async def oci_client(opts, environment=None, runner=None):
    runner = runner or _run
    env = {k: v for k, v in (os.environ if environment is None else environment).items()
           if not k.startswith(('COLORS_PAR_', 'TF_', 'TOFU_'))}
    async def request(method, path, body=None, query=None, headers=None):
        url = 'https://objectstorage.' + opts['oci-region'] + '.oraclecloud.com' + path
        if query:
            url += '?' + urlencode(query)
        auth = opts.get('oci-auth', 'SecurityToken')
        args = ['oci', 'raw-request', '--no-retry', '--auth', 'security_token' if auth == 'SecurityToken' else 'api_key',
                '--profile', opts.get('oci-config-file-profile', 'DEFAULT'), '--region', opts['oci-region'],
                '--http-method', method, '--target-uri', url, '--request-headers', json.dumps(headers or {})]
        if body is not None:
            args += ['--request-body', json.dumps(body)]
        result = await runner(args, os.getcwd(), env, 120000)
        if result.exit:
            raise ValueError('OCI request failed')
        response = json.loads(result.out)
        code = int(response['status'].split()[0])
        if code == 404:
            return None
        if code in (409, 412):
            return {'conflict': True}
        if not 200 <= code < 300:
            raise ValueError(f'OCI request failed ({code})')
        return {'data': response.get('data'), 'headers': {k.lower(): v for k, v in response.get('headers', {}).items()}}
    return request
