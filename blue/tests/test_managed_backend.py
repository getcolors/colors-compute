import json
from pathlib import Path
import pytest
from colors_compute.backend import ProcessResult
from colors_compute.managed_backend import bootstrap_backend, finalize_backend, backend_presence, MARKER

OPTS = {'provider-backend': 's3', 'provider-compute': 'aws', 's3-bucket-mode': 'managed',
        's3-bucket': 'demo-state-123456789012-us-east-1', 's3-region': 'us-east-1',
        'profile': 'demo', 'compute-prevent-destroy': False}
IDENTITY = {'account': '123456789012', 'bucket': OPTS['s3-bucket'], 'region': 'us-east-1', 'profile': 'demo'}

class AWS:
    def __init__(self, exists=False):
        self.exists = exists
        self.calls = []
        self.marker = {'schema': 1, 'identity': IDENTITY.copy(), 'status': 'active'}
        self.objects = {MARKER: self.marker, 'demo/compute/coordination.json': {}}
        self.versions = []
        self.failure = None
        self.foreign = False
        self.phase = None

    async def __call__(self, args, cwd, env, timeout):
        op = args[2]
        self.calls.append(op)
        assert not any(k.startswith('COLORS_PAR_') for k in env)
        value = {}
        if op == self.failure:
            return ProcessResult(1, '', 'An error occurred (AccessDenied) when calling the HeadBucket operation: denied')
        if op == 'get-caller-identity': value = {'Account': IDENTITY['account']}
        elif op == 'head-bucket' and not self.exists:
            return ProcessResult(1, '', 'An error occurred (404) when calling the HeadBucket operation: missing')
        elif op == 'create-bucket': self.exists = True
        elif op == 'get-bucket-tagging': value = {'TagSet': [{'Key': 'colors:profile', 'Value': 'foreign' if self.foreign else 'demo'}, {'Key': 'colors:owner', 'Value': IDENTITY['account']}, {'Key': 'colors:purpose', 'Value': 'managed-backend'}]}
        elif op == 'put-bucket-tagging':
            tags = json.loads(args[args.index('--tagging') + 1])['TagSet']
            self.phase = next((t['Value'] for t in tags if t['Key'] == 'colors:phase'), None)
        elif op == 'get-object':
            key = args[args.index('--key') + 1]
            if key not in self.objects:
                return ProcessResult(1, '', 'An error occurred (NoSuchKey) when calling the GetObject operation: missing')
            Path(args[args.index('--key') + 2]).write_text(json.dumps(self.objects[key]))
            value = {'ETag': '"owner-etag"'}
        elif op == 'put-object':
            self.marker = json.loads(Path(args[args.index('--body') + 1]).read_text())
            self.objects[MARKER] = self.marker
            assert '--if-match' in args or '--if-none-match' in args
        elif op == 'list-objects-v2': value = {'Contents': [{'Key': k} for k in self.objects]}
        elif op == 'list-object-versions': value = {'Versions': self.versions.copy()}
        elif op == 'delete-objects':
            batch = json.loads(args[args.index('--delete') + 1])['Objects']
            self.versions = [v for v in self.versions if v not in batch]
        if op == 'get-bucket-tagging' and self.phase:
            value['TagSet'].append({'Key': 'colors:phase', 'Value': self.phase})
        return ProcessResult(0, json.dumps(value))

class Owner:
    phase = 'retired'
    released = False
    async def acquire(self): pass
    async def snapshot(self): return {'document': {'status': self.phase}}
    async def release(self): self.released = True

@pytest.mark.asyncio
async def test_create_protects_and_is_idempotent():
    aws = AWS()
    assert (await bootstrap_backend(OPTS, {}, aws))['status'] == 'ready'
    assert (await bootstrap_backend(OPTS, {}, aws))['status'] == 'ready'
    assert aws.calls.count('create-bucket') == 1
    assert aws.calls.count('put-bucket-versioning') == 2
    assert 'put-public-access-block' in aws.calls and 'put-bucket-encryption' in aws.calls

@pytest.mark.asyncio
async def test_access_denied_never_creates():
    aws = AWS(); aws.failure = 'head-bucket'
    with pytest.raises(ValueError): await bootstrap_backend(OPTS, {}, aws)
    assert 'create-bucket' not in aws.calls

@pytest.mark.asyncio
async def test_foreign_bucket_is_never_adopted():
    aws = AWS(True); aws.foreign = True
    with pytest.raises(ValueError): await bootstrap_backend(OPTS, {}, aws)
    assert not any(c.startswith('put-') or c.startswith('delete-') for c in aws.calls)

@pytest.mark.asyncio
async def test_missing_existing_backend_never_recreated():
    aws = AWS()
    with pytest.raises(ValueError): await bootstrap_backend({**OPTS, 'compute-require-existing-state': True}, {}, aws)
    assert 'create-bucket' not in aws.calls

@pytest.mark.asyncio
async def test_build_and_external_never_call_aws():
    aws = AWS()
    assert await bootstrap_backend({**OPTS, 'blue/event': 'build'}, {}, aws) == {'status': 'skipped'}
    assert await bootstrap_backend({**OPTS, 's3-bucket-mode': 'external'}, {}, aws) == {'status': 'skipped'}
    assert not aws.calls

@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['active-compute', 'live-state', 'foreign-object', 'lock'])
async def test_finalize_refuses_unfinished_or_foreign(problem):
    aws = AWS(True); owner = Owner()
    if problem == 'active-compute': owner.phase = 'active'
    elif problem == 'live-state': aws.objects['demo/dns.tfstate'] = {'version': 4, 'resources': [{'instances': [{}]}]}
    elif problem == 'foreign-object': aws.objects['foreign/data'] = {}
    else: aws.objects['demo/dns.tfstate.tflock'] = {}
    with pytest.raises(ValueError): await finalize_backend(OPTS, {}, aws, lambda *a, **kw: owner)
    assert 'delete-objects' not in aws.calls and 'delete-bucket' not in aws.calls
    assert owner.released

@pytest.mark.asyncio
async def test_finalize_purges_history_after_retirement_and_marker_last():
    aws = AWS(True); owner = Owner()
    aws.objects['demo/dns.tfstate'] = {'version': 4, 'resources': []}
    aws.versions = [{'Key': 'demo/dns.tfstate', 'VersionId': str(i)} for i in range(1002)] + [{'Key': MARKER, 'VersionId': 'marker'}]
    assert await finalize_backend(OPTS, {}, aws, lambda *a, **kw: owner) == {'status': 'destroyed'}
    assert aws.calls.count('delete-objects') == 3
    assert not owner.released  # its journal has been deleted
    assert aws.marker['status'] == 'deleting'

@pytest.mark.asyncio
async def test_delete_intent_blocks_bootstrap_and_allows_purge_resume():
    aws = AWS(True); aws.marker['status'] = 'deleting'
    with pytest.raises(ValueError): await bootstrap_backend(OPTS, {}, aws)
    assert await finalize_backend(OPTS, {}, aws) == {'status': 'destroyed'}

@pytest.mark.asyncio
async def test_delete_absent_bucket_is_noop():
    aws = AWS()
    assert await finalize_backend(OPTS, {}, aws) == {'status': 'absent'}
    assert 'create-bucket' not in aws.calls

@pytest.mark.asyncio
async def test_finalize_resumes_after_marker_purge_with_durable_deleting_tag():
    aws = AWS(True); aws.objects.pop(MARKER); aws.phase = 'deleting'
    with pytest.raises(ValueError): await bootstrap_backend(OPTS, {}, aws)
    assert await finalize_backend(OPTS, {}, aws) == {'status': 'destroyed'}

@pytest.mark.asyncio
async def test_marker_absent_without_deleting_receipt_refuses():
    aws = AWS(True); aws.objects.pop(MARKER)
    with pytest.raises(ValueError): await finalize_backend(OPTS, {}, aws)
    assert 'delete-bucket' not in aws.calls


@pytest.mark.asyncio
async def test_presence_is_read_only():
    missing = AWS()
    assert await backend_presence(OPTS, {}, missing) == {'status': 'absent'}
    assert missing.calls == ['get-caller-identity', 'head-bucket']
    assert await backend_presence({**OPTS, 's3-bucket-mode': 'external'}, {}, missing) == {'status': 'skipped'}
    assert await backend_presence({**OPTS, 'blue/dry-run': True}, {}, missing) == {'status': 'skipped'}
    existing = AWS(True)
    assert await backend_presence(OPTS, {}, existing) == {'status': 'present'}
    assert existing.calls == ['get-caller-identity', 'head-bucket']
