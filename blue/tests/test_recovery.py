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
