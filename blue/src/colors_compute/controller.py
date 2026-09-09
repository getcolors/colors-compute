"""Library-owned Kubernetes cloud-controller task artifact and compatibility."""
from importlib.resources import files
import json
import re
from ._copy import deepcopy


def controller_artifact(opts, shared):
    descriptors=json.loads(files('colors_compute').joinpath('controllers.json').read_text())
    provider=opts.get('provider-compute')
    if not isinstance(provider,str) or provider not in descriptors:
        raise ValueError('unsupported compute Kubernetes controller capability')
    descriptor=descriptors[provider]
    if descriptor['enabled_option'] in opts and opts[descriptor['enabled_option']] is not True:
        raise ValueError('Kubernetes controller must be enabled')
    version=next((opts.get(key) for key in descriptor['version_options'] if opts.get(key) is not None),None)
    if not isinstance(version,str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',version):
        raise ValueError('invalid Kubernetes controller version')
    params=shared.get('params') if isinstance(shared,dict) else None
    network=params.get(descriptor['network_output']) if isinstance(params,dict) else None
    if not isinstance(params,dict) or params.get('provider')!=provider or not isinstance(network,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',network):
        raise ValueError('invalid Kubernetes controller shared network')
    name=opts.get(provider+'-name')
    if name is None:
        name=opts.get('profile')
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}',name):
        raise ValueError('invalid Kubernetes controller cluster name')
    return {'filename':'colors-compute-controller.yml',
        'content':descriptor['content'].replace('@VERSION@',version).replace('@NETWORK_ID@',network),
        'credentials':deepcopy(descriptor['credentials']), 'namespace':descriptor['namespace'],
        'rollout_resource':descriptor['rollout_resource'],'cluster_name':name}
