"""Explicit recovery for an AWS shared create with no surviving state/resources."""
import json
import os
import tempfile
from .backend import _run
from .coordinator import Coordinator
from .execution import state_presence

SCANS = [('describe-vpcs', 'Vpcs', 'tag:Name'), ('describe-subnets', 'Subnets', 'tag:Name'),
         ('describe-internet-gateways', 'InternetGateways', 'tag:Name'),
         ('describe-route-tables', 'RouteTables', 'tag:Name'),
         ('describe-security-groups', 'SecurityGroups', 'tag:Name'),
         ('describe-key-pairs', 'KeyPairs', 'key-name')]


async def recover_absent_aws_shared(opts, operation_id, environment=None, runner=None, coordinator_factory=None):
    """Operator-invoked only; bind review to failed operation_id, never auto-retry."""
    if opts.get('provider-compute') != 'aws':
        raise ValueError('recovery requires AWS')
    env = dict(os.environ if environment is None else environment)
    owner = (coordinator_factory or Coordinator)(opts, env, event_prefix='lifecycle/')
    await owner.acquire()
    try:
        doc = (await owner.snapshot())['document']
        shared = doc['shared']
        if not (doc['status'] == 'active' and doc['key']['phase'] == 'prepared'
                and shared['phase'] == 'failed' and shared['operation'] == 'create'
                and shared['operation_id'] == operation_id and all(n['phase'] == 'declared' for n in doc['nodes'].values())):
            raise ValueError('recovery does not match failed initial create')
        if await state_presence(opts, opts['profile'] + '/compute/shared.tfstate', env, runner) != {'status': 'absent'}:
            raise ValueError('recovery requires confirmed absent shared state')
        child = {k: v for k, v in env.items() if not k.startswith(('COLORS_PAR_', 'TF_', 'TOFU_'))}
        child.update(AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off', AWS_MAX_ATTEMPTS='1')
        with tempfile.TemporaryDirectory(prefix='colors-recovery-') as directory:
            for operation, collection, filter_name in SCANS:
                args = ['aws', 'ec2', operation, '--region', opts['aws-region'], '--filters',
                        f"Name={filter_name},Values={opts['profile']}*", '--query', f'length({collection})', '--output', 'json', '--no-cli-pager']
                result = await (runner or _run)(args, directory, child, 120000)
                if result.exit != 0 or json.loads(result.out) != 0:
                    raise ValueError('recovery requires reviewed absent AWS resources')
        await owner.transition('shared-retry', evidence='verified-provider-absence')
        return {'status': 'recovered'}
    finally:
        await owner.release()


async def recover_absent_oci_nodes(opts, operations, environment=None, runner=None, coordinator_factory=None):
    """Recover exact failed OCI operations after native state and resource audits."""
    from .oci import oci_client, object_path, availability_domains
    from .backend import _params
    if opts.get('provider-compute') != 'oci' or opts.get('provider-backend') != 'oci' or not isinstance(operations, dict) or not operations:
        raise ValueError('recovery requires OCI node operation IDs')
    env = dict(os.environ if environment is None else environment)
    owner = (coordinator_factory or Coordinator)(opts, env, event_prefix='lifecycle/')
    await owner.acquire()
    try:
        doc = (await owner.snapshot())['document']
        if doc['status'] != 'active' or doc['key']['phase'] != 'prepared' or doc['shared']['phase'] != 'ready':
            raise ValueError('recovery requires owned OCI shared state')
        request = await oci_client(opts, env, runner)
        for node_id, operation_id in operations.items():
            record = doc['nodes'].get(node_id, {})
            if record.get('phase') != 'failed' or record.get('operation') != 'create' or record.get('operation_id') != operation_id:
                raise ValueError('recovery does not match failed node create')
            observed = await request('GET', object_path(opts, record['state_key']))
            if observed is not None:
                state = observed.get('data')
                _params(json.dumps(state))
                if state['resources'] != [] or state['outputs'] != {}:
                    raise ValueError('recovery requires absent or empty node state')
        request = await oci_client(opts, env, runner, 'iaas')
        domains = list(dict.fromkeys([opts['oci-availability-domain']] + availability_domains(opts)))
        scopes = [('/20160918/instances', {})] + [('/20160918/bootVolumes', {'availabilityDomain': domain}) for domain in domains]
        for path, filters in scopes:
            page = None
            while True:
                response = await request('GET', path, query={**filters, 'compartmentId': opts['oci-compartment-id'], **({'page': page} if page else {})})
                data = response.get('data') if response else None
                if not isinstance(data, list) or any(opts['profile'] in r.get('displayName', '') and r.get('lifecycleState') != 'TERMINATED' for r in data):
                    raise ValueError('recovery requires absent OCI instances and boot volumes')
                page = response['headers'].get('opc-next-page')
                if not page:
                    break
        for node_id in operations:
            await owner.transition('retry', node_id=node_id, evidence='verified-provider-absence')
        return {'status': 'recovered', 'nodes': sorted(operations)}
    finally:
        await owner.release()
