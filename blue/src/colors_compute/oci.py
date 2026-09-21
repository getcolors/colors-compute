"""OCI node placement and CPU validation."""
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
