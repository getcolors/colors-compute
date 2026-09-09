"""Pure schema-3 managed cluster ownership; no SSH or VM node semantics."""
from ._copy import deepcopy
from .coordination import _identity, _safe, _shape, _nonblank, _integer, _observation
MAXIMUM=9007199254740991


def managed_document_valid(doc):
    if not _shape(doc, ('schema_version','kind','identity','revision','write_id','lock','generation','status','shared')) or doc['schema_version'] != 3 or doc['kind'] != 'managed-kubernetes' or not _identity(doc['identity']) or not _integer(doc['revision'],1,MAXIMUM) or not _integer(doc['generation'],1,MAXIMUM) or not _safe(doc['write_id']):return False
    lock=doc['lock'];record=doc['shared']
    if not _shape(lock,('state','run_id')) or not (lock['state']=='held' and _safe(lock['run_id']) or lock=={'state':'idle','run_id':None}):return False
    if doc['status'] not in ('active','retired') or not _shape(record,('phase','operation','operation_id')) or record['phase'] not in ('declared','running','ready','failed','destroyed'):return False
    if record['phase']=='declared':
        if record['operation'] is not None or record['operation_id'] is not None:return False
    elif record['operation'] not in ('create','destroy') or not _safe(record['operation_id']):return False
    if record['phase']=='ready' and record['operation']!='create' or record['phase']=='destroyed' and record['operation']!='destroy':return False
    if record['phase']=='running' and lock['state']!='held':return False
    return doc['status']!='retired' or record['phase']=='destroyed'


def managed_coordination(observed,identity,event):
    def require(value,message='managed transition refused'):
        if not value:raise ValueError(message)
    require(_identity(identity),'invalid coordination identity')
    require(isinstance(event,dict) and isinstance(event.get('type'),str) and event['type'].startswith('managed/'),'invalid managed event')
    kind=event['type'][8:]
    extra={'acquire':(), 'release':(), 'recreate':(), 'shared-start':('operation_id',),'shared-destroy':('operation_id',),'shared-complete':('operation_id',),'shared-fail':('operation_id',),'shared-retry':('evidence',)}
    require(kind in extra and _shape(event,('type','run_id','write_id','target_etag',*extra[kind])) and _safe(event['run_id']) and _safe(event['write_id']) and (event['target_etag'] is None or _nonblank(event['target_etag'])),'invalid managed event')
    if 'operation_id' in event:require(_safe(event['operation_id']),'invalid managed event')
    if kind=='shared-retry':require(event['evidence']=='readable-state','invalid managed event')
    require(_observation(observed),'invalid coordination observation');require(observed['status']!='error','coordination read failed')
    present=observed['status']=='present';doc=observed.get('document')
    if present:
        require(managed_document_valid(doc),'invalid managed document');require(doc['identity']==identity,'coordination identity mismatch')
        require(doc['write_id']!=event['write_id'],'coordination write_id reused');require(doc['revision']<MAXIMUM,'coordination revision exhausted')
    require(event['target_etag']==(observed['etag'] if present else None),'stale coordination observation')
    if not present:
        require(kind=='acquire','coordination object absent')
        return {'condition':{'if_none_match':'*'},'document':{'schema_version':3,'kind':'managed-kubernetes','identity':deepcopy(identity),'revision':1,'write_id':event['write_id'],'lock':{'state':'held','run_id':event['run_id']},'generation':1,'status':'active','shared':{'phase':'declared','operation':None,'operation_id':None}}}
    doc=deepcopy(doc);record=doc['shared']
    if kind=='acquire':require(doc['lock']['state']=='idle','coordination lock held');doc['lock']={'state':'held','run_id':event['run_id']}
    else:
        require(doc['lock']=={'state':'held','run_id':event['run_id']},'coordination owner mismatch')
        if kind=='release':require(record['phase']!='running','coordination operations outstanding');doc['lock']={'state':'idle','run_id':None}
        elif kind=='recreate':
            require(doc['status']=='retired' and doc['generation']<MAXIMUM);doc.update(status='active',generation=doc['generation']+1,shared={'phase':'declared','operation':None,'operation_id':None})
        elif kind in ('shared-start','shared-destroy'):
            operation='create' if kind=='shared-start' else 'destroy';allowed=('declared','ready') if operation=='create' else ('declared','ready','failed')
            require(doc['status']=='active' and record['phase'] in allowed and event['operation_id']!=record['operation_id']);record.update(phase='running',operation=operation,operation_id=event['operation_id'])
        elif kind in ('shared-complete','shared-fail'):
            require(record['phase']=='running' and record['operation_id']==event['operation_id']);record['phase']='failed' if kind=='shared-fail' else 'ready' if record['operation']=='create' else 'destroyed'
            if record['phase']=='destroyed':doc['status']='retired'
        elif kind=='shared-retry':
            require(doc['status']=='active' and record['phase']=='failed' and record['operation']=='create');doc['shared']={'phase':'declared','operation':None,'operation_id':None}
    doc.update(revision=doc['revision']+1,write_id=event['write_id']);require(managed_document_valid(doc),'invalid managed transition result')
    return {'condition':{'if_match':observed['etag']},'document':doc}
