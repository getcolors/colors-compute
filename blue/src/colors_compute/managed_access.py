"""Validated managed-cluster outputs and a fixed private kubeconfig sink."""
import base64
import binascii
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import yaml
from urllib.parse import urlsplit

from .backend import _params
from .contract import _safe


def managed_kubeconfig_path(opts):
    workdir, profile = opts.get('workdir'), opts.get('profile')
    if not isinstance(workdir, str) or not workdir.strip() or not _safe(profile):
        raise ValueError('invalid managed access path')
    return str(Path(workdir).absolute() / profile / 'kubeconfig')


def public_params(value, provider):
    required = {'provider', 'kind', 'name', 'cluster_id', 'endpoint'}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'pod_cidr', 'service_cidr'}:
        raise ValueError('invalid managed outputs')
    if value['provider'] != provider or value['kind'] != 'managed-kubernetes' or not _safe(value['name']):
        raise ValueError('invalid managed outputs')
    if not isinstance(value['cluster_id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', value['cluster_id']):
        raise ValueError('invalid managed outputs')
    endpoint = value['endpoint']
    if not isinstance(endpoint, str):
        raise ValueError('invalid managed outputs')
    url = urlsplit(endpoint)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment or url.query:
        raise ValueError('invalid managed outputs')
    for key in ('pod_cidr', 'service_cidr'):
        if key in value and (not isinstance(value[key], str) or ipaddress.ip_network(value[key], strict=True).version != 4 or str(ipaddress.ip_network(value[key], strict=True)) != value[key]):
            raise ValueError('invalid managed outputs')
    return dict(value)


class AccessDecoder:
    def __init__(self, opts, environment=None):
        self.opts = opts
        env = os.environ if environment is None else environment
        from .contract import registry
        keys = list(registry()["compute"][opts["provider-compute"]]["tofu-env"])
        self._secrets = [env.get("COLORS_PAR_" + key.upper().replace("-", "_")) for key in keys + list(registry()["backend"].get(opts.get("provider-backend"), {}).get("secrets", []))]
        self._secrets = [value for value in self._secrets if isinstance(value, str) and value]
        self._content = None

    def __call__(self, text):
        params = public_params(_params(text), self.opts['provider-compute'])
        outputs = json.loads(text)['outputs']
        if set(outputs) != {'params', 'kubeconfig_b64'}:
            raise ValueError('invalid managed outputs')
        if outputs['params'].get('sensitive', False) is not False:
            raise ValueError('invalid managed outputs')
        entry = outputs['kubeconfig_b64']
        if not isinstance(entry, dict) or entry.get('sensitive') is not True or not isinstance(entry.get('value'), str) or len(entry['value']) > 2796204:
            raise ValueError('invalid managed outputs')
        try:
            raw = base64.b64decode(entry['value'], validate=True)
            content = raw.decode('utf-8')
        except (ValueError, UnicodeError, binascii.Error):
            raise ValueError('invalid managed outputs') from None
        if base64.b64encode(raw).decode('ascii') != entry['value'] or not raw or len(raw) > 2097152 or '\x00' in content or content.startswith('\ufeff'):
            raise ValueError('invalid managed outputs')
        if any(secret in content or json.dumps(secret)[1:-1] in content for secret in self._secrets):
            raise ValueError('invalid managed outputs')
        # Parse both YAML and JSON forms; flow mappings cannot bypass this guard.
        config = yaml.safe_load(content)
        if not isinstance(config, dict) or config.get('kind') != 'Config' or config.get('apiVersion') != 'v1':
            raise ValueError('invalid managed outputs')
        clusters = config.get('clusters')
        if not isinstance(clusters, list) or len(clusters) != 1 or not isinstance(clusters[0], dict):
            raise ValueError('invalid managed outputs')
        cluster = clusters[0].get('cluster')
        if not isinstance(cluster, dict) or cluster.get('server', '').rstrip('/') != params['endpoint'].rstrip('/') or cluster.get('insecure-skip-tls-verify', False) is not False:
            raise ValueError('invalid managed outputs')
        contexts, users = config.get('contexts'), config.get('users')
        if not isinstance(contexts, list) or len(contexts) != 1 or not isinstance(users, list) or len(users) != 1:
            raise ValueError('invalid managed outputs')
        context, user = contexts[0], users[0]
        if not isinstance(context, dict) or not isinstance(user, dict) or not isinstance(context.get('context'), dict) or not isinstance(user.get('user'), dict):
            raise ValueError('invalid managed outputs')
        names = [clusters[0].get('name'), context.get('name'), user.get('name')]
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError('invalid managed outputs')
        if config.get('current-context') != names[1] or context['context'].get('cluster') != names[0] or context['context'].get('user') != names[2]:
            raise ValueError('invalid managed outputs')
        credentials = user['user']
        token = isinstance(credentials.get('token'), str) and bool(credentials['token'].strip())
        certificate = all(isinstance(credentials.get(key), str) and bool(credentials[key].strip()) for key in ('client-certificate-data', 'client-key-data'))
        if not (token or certificate):
            raise ValueError('invalid managed outputs')
        forbidden = {'exec', 'auth-provider', 'tokenFile', 'client-certificate', 'client-key', 'certificate-authority', 'proxy-url', 'tls-server-name'}
        seen, pending = set(), [config]
        while pending:
            value = pending.pop()
            if isinstance(value, (dict, list)):
                if id(value) in seen:
                    raise ValueError('invalid managed outputs')
                seen.add(id(value))
                if len(seen) > 10000:
                    raise ValueError('invalid managed outputs')
                if isinstance(value, dict):
                    if any(not isinstance(key, str) or key in forbidden for key in value):
                        raise ValueError('invalid managed outputs')
                    pending.extend(value.values())
                else:
                    pending.extend(value)
        self._content = raw
        return {'params': params}

    def write(self):
        if self._content is None:
            raise ValueError('managed access unavailable')
        target = Path(managed_kubeconfig_path(self.opts))
        # Reject every symlink ancestor, including a preexisting target.
        for part in [*reversed(target.parents), target]:
            if part.is_symlink():
                raise ValueError('invalid managed access path')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists() and (not target.is_file() or target.stat().st_uid != os.getuid()):
            raise ValueError('invalid managed access path')
        fd, temporary = tempfile.mkstemp(prefix='.kubeconfig-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(self._content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
            self._content = None
        return str(target)
