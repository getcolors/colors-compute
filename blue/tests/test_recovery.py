import json
import pytest
from colors_compute.backend import ProcessResult
from colors_compute.recovery import recover_absent_aws_shared
OPTS = {'profile': 'demo', 'provider-compute': 'aws', 'provider-backend': 's3', 's3-bucket': 'states', 's3-region': 'us-east-1', 'aws-region': 'us-east-1'}
class Owner:
    def __init__(self):
        self.doc = {'status': 'active', 'key': {'phase': 'prepared'}, 'shared': {'phase': 'failed', 'operation': 'create', 'operation_id': 'attempt'}, 'nodes': {'0': {'phase': 'declared'}}}
        self.transitions = []; self.released = False
    async def acquire(self): pass
    async def snapshot(self): return {'document': self.doc}
    async def transition(self, *args, **kw): self.transitions.append((args, kw))
    async def release(self): self.released = True
class Runner:
    def __init__(self): self.calls = []; self.resources = 0; self.state = False; self.error = False
    async def __call__(self,args,cwd,env,timeout):
        self.calls.append(args)
        if args[1] == 's3api':
            if self.state: return ProcessResult(0, '{"ETag":"etag"}')
            return ProcessResult(1, '', 'An error occurred (NoSuchKey) when calling the GetObject operation: missing')
        return ProcessResult(1 if self.error else 0, json.dumps(self.resources), '')
@pytest.mark.asyncio
async def test_recovery_checks_aws_scope_and_records_evidence():
    owner=Owner();runner=Runner()
    assert await recover_absent_aws_shared(OPTS,'attempt',{},runner,lambda *a,**kw:owner)=={'status':'recovered'}
    assert len(runner.calls)==7 and owner.released
    assert owner.transitions==[(('shared-retry',),{'evidence':'verified-provider-absence'})]
@pytest.mark.asyncio
@pytest.mark.parametrize('case',['wrong-attempt','node-started','state-present','resource-found','aws-denied'])
async def test_recovery_refuses_uncertain_or_surviving_resources(case):
    owner=Owner();runner=Runner();attempt='attempt'
    if case=='wrong-attempt':attempt='different'
    elif case=='node-started':owner.doc['nodes']['0']['phase']='ready'
    elif case=='state-present':runner.state=True
    elif case=='resource-found':runner.resources=1
    else:runner.error=True
    with pytest.raises(ValueError):await recover_absent_aws_shared(OPTS,attempt,{},runner,lambda *a,**kw:owner)
    assert owner.released and not owner.transitions

@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['empty', 'absent', 'active-state', 'instance', 'boot-volume', 'terminating', 'wrong-attempt', 'denied', 'second-page'])
async def test_oci_recovery_matches_operation_and_audits_provider(case):
    from colors_compute.recovery import recover_absent_oci_nodes
    from urllib.parse import urlparse, parse_qs
    opts = {'provider-compute': 'oci', 'provider-backend': 'oci', 'profile': 'demo', 'oci-region': 'eu-frankfurt-1', 'oci-namespace': 'namespace', 'oci-bucket': 'states', 'oci-compartment-id': 'ocid1.compartment.example', 'oci-availability-domain': 'ad1', 'oci-availability-domains': ['ad1', 'ad2', 'ad3']}
    owner = Owner(); owner.doc['shared'] = {'phase': 'ready'}
    owner.doc['nodes'] = {'0': {'phase': 'failed', 'operation': 'create', 'operation_id': 'attempt', 'state_key': 'demo/compute/nodes/0.tfstate'}}
    domains = []
    async def runner(args, cwd, env, timeout):
        assert args[:2] == ['oci', 'raw-request']
        url = urlparse(args[args.index('--target-uri')+1]); query = parse_qs(url.query)
        if '/o/' in url.path:
            state = {'version': 4, 'serial': 1, 'lineage': 'line', 'resources': [{}] if case == 'active-state' else [], 'outputs': {}}
            return ProcessResult(0, json.dumps({'status': '404 Not Found' if case == 'absent' else '200 OK', 'data': state, 'headers': {}}))
        assert query['compartmentId'] == ['ocid1.compartment.example']
        if case == 'denied': return ProcessResult(0, json.dumps({'status': '403 Forbidden'}))
        if 'availabilityDomain' in query: domains.append(query['availabilityDomain'][0])
        data = []
        if case in ('instance', 'boot-volume', 'terminating'):
            data = [{'displayName': 'Boot volume of instance demo-0' if case == 'boot-volume' else 'demo-0', 'lifecycleState': 'TERMINATING' if case == 'terminating' else 'RUNNING'}]
        if case == 'second-page':
            if 'page' not in query:
                return ProcessResult(0, json.dumps({'status': '200 OK', 'data': [], 'headers': {'opc-next-page': 'next'}}))
            assert query['page'] == ['next']
            data = [{'displayName': 'Boot volume of instance demo-0', 'lifecycleState': 'AVAILABLE'}]
        return ProcessResult(0, json.dumps({'status': '200 OK', 'data': data, 'headers': {}}))
    operations = {'0': 'other' if case == 'wrong-attempt' else 'attempt'}
    if case in ('empty', 'absent'):
        assert await recover_absent_oci_nodes(opts, operations, {}, runner, lambda *a, **kw: owner) == {'status': 'recovered', 'nodes': ['0']}
        assert owner.transitions == [(('retry',), {'node_id': '0', 'evidence': 'verified-provider-absence'})]
        assert domains == ['ad1', 'ad2', 'ad3']
    else:
        with pytest.raises(ValueError): await recover_absent_oci_nodes(opts, operations, {}, runner, lambda *a, **kw: owner)
        assert not owner.transitions
    assert owner.released
