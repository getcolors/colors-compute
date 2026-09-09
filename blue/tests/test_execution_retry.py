import json
import pytest
from colors_compute.backend import ProcessResult
from colors_compute.execution import converge_state
OPTS={'profile':'demo','provider-compute':'digitalocean','provider-backend':'s3','s3-bucket':'states','s3-region':'us-east-1','compute-prevent-destroy':False}
DOCS={'shared.tf.json':{'resource':{'digitalocean_vpc':{'cluster':{'name':'demo'}}}}}
STATE={'version':4,'serial':1,'lineage':'lineage','resources':[{'type':'digitalocean_vpc'}],'outputs':{'params':{'value':{'provider':'digitalocean'}}}}
EMPTY={**STATE,'resources':[],'outputs':{}}
PLAN={'format_version':'1.2','planned_values':{},'resource_changes':[{'change':{'actions':['delete']}}]}
ENV={'COLORS_PAR_DO_TOKEN':'fixture-do-token'}
@pytest.mark.asyncio
@pytest.mark.parametrize('failures,message,expected,attempts',[(1,'Can not delete VPC with members','destroyed',2),(9,'Can not delete VPC with members','error',4),(1,'unclassified provider failure','error',1),(1,'Can not delete VPC with members fixture-do-token','error',1)])
async def test_only_classified_vpc_destroy_retries(failures,message,expected,attempts):
    calls=[];waits=[];applied=False;count=0
    async def run(args,*_):
        nonlocal applied,count
        calls.append(args)
        if args[1]=='state': return ProcessResult(0,json.dumps(EMPTY if applied else STATE),'')
        if args[1]=='show': return ProcessResult(0,json.dumps(PLAN),'')
        if args[1]=='apply':
            count+=1
            if count<=failures:return ProcessResult(1,'',message)
            applied=True
        return ProcessResult(0,'','')
    async def sleep(value):waits.append(value)
    assert await converge_state(OPTS,'demo/compute/shared.tfstate',DOCS,'delete',{'status':'present'},ENV,run,sleep)=={'status':expected}
    assert count==attempts and waits==[30]*(attempts-1)
    assert sum(c[1]=='plan' for c in calls)==attempts
    if attempts>1:
        first=next(i for i,c in enumerate(calls) if c[1]=='apply')
        assert calls[first+1][1:]==['state','pull'] and calls[first+2][1]=='plan'

@pytest.mark.asyncio
async def test_retry_refuses_unreadable_partial_state():
    calls=[]
    async def run(args,*_):
        calls.append(args[1])
        if args[1]=='state':return ProcessResult(0,'invalid' if 'apply' in calls else json.dumps(STATE),'')
        if args[1]=='show':return ProcessResult(0,json.dumps(PLAN),'')
        if args[1]=='apply':return ProcessResult(1,'','Can not delete VPC with members')
        return ProcessResult(0,'','')
    async def sleep(_):raise AssertionError('cannot retry uncertain state')
    assert await converge_state(OPTS,'demo/compute/shared.tfstate',DOCS,'delete',{'status':'present'},ENV,run,sleep)=={'status':'error'}
    assert calls.count('apply')==1
