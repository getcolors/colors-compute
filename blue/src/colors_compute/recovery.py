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
