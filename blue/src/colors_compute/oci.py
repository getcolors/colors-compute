"""OCI Object Storage requests signed by the operator's OCI CLI profile."""
import json
import os
from urllib.parse import quote, urlencode
from .backend import _run


def bucket_path(opts):
    return '/n/' + quote(opts['oci-namespace'], safe='') + '/b/' + quote(opts['oci-bucket'], safe='')


def object_path(opts, key):
    return bucket_path(opts) + '/o/' + quote(key, safe='')


async def oci_client(opts, environment=None, runner=None, service="objectstorage"):
    if service not in ("objectstorage", "iaas"):
        raise ValueError("invalid OCI service")
    runner = runner or _run
    env = {k: v for k, v in (os.environ if environment is None else environment).items()
           if not k.startswith(('COLORS_PAR_', 'TF_', 'TOFU_'))}
    async def request(method, path, body=None, query=None, headers=None):
        url = 'https://' + service + '.' + opts['oci-region'] + '.oraclecloud.com' + path
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


def availability_domains(opts):
    import re
    if 'oci-availability-domains' not in opts:
        return [opts.get('oci-availability-domain')]
    domains = opts['oci-availability-domains']
    if (not isinstance(domains, list) or not 1 <= len(domains) <= 16
            or not all(isinstance(d, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:_-]{0,255}', d) for d in domains)
            or len(set(domains)) != len(domains)):
        raise ValueError('invalid OCI availability domains')
    return domains


def place_node(opts, stage, node_id):
    import re
    if opts.get("provider-compute") == "oci" and stage == "node":
        validate_cpus(opts)
    if opts.get('provider-compute') != 'oci' or 'oci-availability-domains' not in opts:
        return opts
    domains = availability_domains(opts)
    if stage != 'node':
        return opts
    index = re.search(r'(?:^|-)(0|[1-9][0-9]{0,2})$', node_id) if isinstance(node_id, str) else None
    if not index:
        raise ValueError('OCI placement requires an indexed node ID')
    return {**opts, 'oci-availability-domain': domains[int(index[1]) % len(domains)]}


def validate_cpus(opts):
    import math
    ocpus, vcpus = opts.get('oci-ocpus'), opts.get('oci-vcpus')
    values = [v for v in (ocpus, vcpus) if v is not None]
    if len(values) != 1:
        raise ValueError('OCI requires exactly one of oci-ocpus or oci-vcpus')
    if type(values[0]) not in (int, float) or not math.isfinite(values[0]) or values[0] <= 0 or (vcpus is not None and vcpus != int(vcpus)):
        raise ValueError('invalid OCI CPU count')
