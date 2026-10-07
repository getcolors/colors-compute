"""Mocked read-only SSH consumer verification; never accesses a cloud account."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ssh_adapter', ROOT / 'agents/ssh-resource.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class AbsenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.opts = {'profile':'demo','provider-compute':'google','provider-backend':'local','google-project':'test'}
        self.request = {'workdir':self.tmp.name,'consumers':[{'node_id':'app','state_filename':'node.tfstate'}],'registrations':[]}

    def test_exact_state_and_provider_identities(self):
        with patch.object(a,'absence_state') as state, patch.object(a,'absence_provider') as provider:
            self.assertEqual(a.verify_absent(self.opts,self.request), {'status':'verified','verified_absent':True})
            state.assert_called_once_with(self.opts,self.tmp.name,'app','node.tfstate')
            provider.assert_called_once_with(self.opts,{'demo-app'},set())
        self.opts['provider-compute']='digitalocean'
        self.request['registrations']=[{'name':'access','state_filename':'registration.tfstate'}]
        with patch.object(a,'absence_state') as state, patch.object(a,'absence_provider') as provider:
            a.verify_absent(self.opts,self.request)
            self.assertEqual(state.call_args_list[1].args[-2:],('registration-access','registration.tfstate'))
            provider.assert_called_once_with(self.opts,{'demo-app'},{'demo-registration-access'})

    def test_names_match_real_registration_plans(self):
        sys.path.insert(0,str(ROOT/'blue/src'))
        from colors_compute.node import registration_plan
        cases=json.loads((ROOT/'test/fixtures/provider-requests.json').read_text())
        identity=json.loads((ROOT/'test/fixtures/public-ssh.json').read_text())
        resources={'aws':'aws_key_pair','digitalocean':'digitalocean_ssh_key','hcloud':'hcloud_ssh_key','vultr':'vultr_ssh_key'}
        for provider,resource in resources.items():
            opts=next(c['args'][0] for c in cases if c['args'][0]['provider-compute']==provider and c['args'][1]=='shared')
            opts={**opts,'provider-backend':'local'}
            request={'name':'access','workdir':self.tmp.name,'state_filename':'key.tfstate','ssh_resource':identity}
            plan=registration_plan(opts,request)
            actual=plan['documents']['compute.tf.json']['resource'][resource]['machine']['key_name' if provider=='aws' else 'name']
            verify={**self.request,'registrations':[{'name':'access','state_filename':'key.tfstate'}]}
            with patch.object(a,'absence_state') as state,patch.object(a,'absence_provider') as checked:
                a.verify_absent(opts,verify)
                self.assertEqual(checked.call_args.args[2],{actual})
                self.assertEqual(str(Path(self.tmp.name)/opts['profile']/state.call_args.args[2]),plan['directory'])

    def test_invalid_descriptors_fail_before_io(self):
        variants = [dict(self.request, consumers=[]),dict(self.request,registrations=[{}]),dict(self.request,extra=True),
                    dict(self.request,workdir='/tmp/../bad'),dict(self.request,consumers=[{'node_id':'../bad','state_filename':'x.tfstate'}]),
                    dict(self.request,consumers=self.request['consumers']*2)]
        with patch.object(a,'absence_state') as state:
            for request in variants:
                with self.subTest(request=request),self.assertRaises(a.ResourceError):
                    a.verify_absent(self.opts,request)
            state.assert_not_called()
        for provider in ('aws','digitalocean','hcloud','vultr'):
            with self.subTest(provider=provider),self.assertRaises(a.ResourceError):
                a.verify_absent(dict(self.opts,**{'provider-compute':provider}),self.request)

    def test_state_is_read_before_provider_and_failure_blocks(self):
        with patch.object(a,'absence_state',side_effect=a.ResourceError('failed')),patch.object(a,'absence_provider') as provider:
            with self.assertRaises(a.ResourceError):a.verify_absent(self.opts,self.request)
            provider.assert_not_called()

    def test_state_validation(self):
        empty={'version':4,'serial':0,'lineage':'id','outputs':{},'resources':[]}
        a.absence_empty_state(None)
        a.absence_empty_state(json.dumps(empty))
        invalid=[{},dict(empty,resources=[{}]),dict(empty,outputs={'params':{'value':{}}}),dict(empty,serial=True),dict(empty,lineage=''),dict(empty,version=4.0)]
        for state in invalid:
            with self.subTest(state=state),self.assertRaises(a.ResourceError):a.absence_empty_state(json.dumps(state))
        for raw in ('null','{"version":4,"version":4}', 'NaN'):
            with self.subTest(raw=raw),self.assertRaises(a.ResourceError):a.absence_empty_state(raw)

    def test_local_state_empty_present_and_symlink(self):
        path=Path(self.tmp.name)/'demo'/'app'/'node.tfstate'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'version':4,'serial':1,'lineage':'a','outputs':{},'resources':[]}));path.chmod(0o600)
        a.absence_state(self.opts,self.tmp.name,'app','node.tfstate')
        path.write_text('{}')
        with self.assertRaises(a.ResourceError):a.absence_state(self.opts,self.tmp.name,'app','node.tfstate')
        path.unlink();path.symlink_to('/nonexistent')
        with self.assertRaises(a.ResourceError):a.absence_state(self.opts,self.tmp.name,'app','node.tfstate')

    def test_remote_state_only_explicit_object_absence(self):
        opts=dict(self.opts,**{'provider-backend':'s3','s3-bucket':'test','s3-region':'us-east-1','s3-prefix':'scope'})
        with patch.object(a,'run',return_value=(1,b'',b'An error occurred (NoSuchKey) when calling the GetObject operation')) as run:
            a.absence_state(opts,self.tmp.name,'app','node.tfstate')
            self.assertIn('scope/demo/node.tfstate',run.call_args.args[0])
        for error in (b'AccessDenied',b'NoSuchBucket',b'404',b'timeout'):
            with patch.object(a,'run',return_value=(1,b'',error)),self.assertRaises(a.ResourceError):a.absence_state(opts,self.tmp.name,'app','node.tfstate')

    def test_remote_present_state_and_gcs_workspace(self):
        empty={'version':4,'serial':0,'lineage':'x','resources':[],'outputs':{}}
        opts=dict(self.opts,**{'provider-backend':'gcs','gcs-bucket':'test'})
        calls=[]
        def run(args,*_,**__):
            calls.append(args)
            if 'print-access-token' in args:return 0,b'adc-token',b''
            if 'describe' in args:return 0,b'{"generation":"4"}',b''
            Path(args[-1]).write_text(json.dumps(empty));return 0,b'',b''
        with patch.object(a,'run',side_effect=run):a.absence_state(opts,self.tmp.name,'app','node.tfstate')
        self.assertIn('gs://test/demo/node.tfstate/default.tfstate',calls[1])
        self.assertIn('gs://test/demo/node.tfstate/default.tfstate#4',calls[2])

    def test_google_pagination_and_conflicts(self):
        pages=[{'kind':'compute#instanceAggregatedList','items':{'zones/a':{'warning':{'code':'NO_RESULTS_ON_PAGE'}}},'nextPageToken':'second'},
               {'kind':'compute#instanceAggregatedList','items':{'zones/b':{'instances':[{'name':'other'}]}}}]
        with patch.object(a,'run',return_value=(0,b'adc-token',b'')) as run,patch.object(a,'absence_http',side_effect=pages) as http:
            a.absence_provider(self.opts,{'demo-app'},set())
            self.assertEqual(run.call_args.args[0],['gcloud','auth','application-default','print-access-token'])
            self.assertIn('pageToken=second',http.call_args.args[0])
        bad=[{}, {'kind':'compute#instanceAggregatedList','items':{},'unreachables':['zone']},
             {'kind':'compute#instanceAggregatedList','items':{'zone':{'instances':[{'name':'demo-app'}]}}},
             {'kind':'compute#instanceAggregatedList','items':{'zone':{'warning':{'code':'UNREACHABLE'}}}},
             {'kind':'compute#instanceAggregatedList','items':{'zone':{'instances':[{}]}}},
             {'kind':'compute#instanceAggregatedList','items':{},'nextPageToken':''}]
        for data in bad:
            with self.subTest(data=data),patch.object(a,'run',return_value=(0,b'token',b'')),patch.object(a,'absence_http',return_value=data),self.assertRaises(a.ResourceError):a.absence_provider(self.opts,{'demo-app'},set())
        with patch.object(a,'run',return_value=(0,b'token',b'')),patch.object(a,'absence_http',return_value=pages[0]),self.assertRaises(a.ResourceError):a.absence_provider(self.opts,{'demo-app'},set())

    def test_google_explicit_empty_warning(self):
        empty={'kind':'compute#instanceAggregatedList','warning':{'code':'NO_RESULTS_ON_PAGE'}}
        for data in (empty,dict(empty,items={})):
            with patch.object(a,'run',return_value=(0,b'token',b'')),patch.object(a,'absence_http',return_value=data):
                a.absence_provider(self.opts,{'demo-app'},set())
        for data in (dict(empty,warning={'code':'UNREACHABLE'}),dict(empty,unreachables=['zone']),
                     dict(empty,items={'zone':{'instances':[{'name':'other'}]}}),dict(empty,items=None),
                     dict(empty,error={'message':'partial'}),{'kind':'compute#instanceAggregatedList'}):
            with self.subTest(data=data),patch.object(a,'run',return_value=(0,b'token',b'')),patch.object(a,'absence_http',return_value=data),self.assertRaises(a.ResourceError):
                a.absence_provider(self.opts,{'demo-app'},set())
        pages=[dict(empty,nextPageToken='next'),{'kind':'compute#instanceAggregatedList','items':{'zone':{'instances':[{'name':'demo-app'}]}}}]
        with patch.object(a,'run',return_value=(0,b'token',b'')),patch.object(a,'absence_http',side_effect=pages) as http,self.assertRaises(a.ResourceError):
            a.absence_provider(self.opts,{'demo-app'},set())
        self.assertEqual(http.call_count,2)

    def test_digitalocean_complete_inventory_and_pagination(self):
        pages=[{'droplets':[{'name':'other'}],'meta':{'total':2},'links':{'pages':{'next':'https://api.digitalocean.com/v2/droplets?page=2&per_page=100'}}},
               {'droplets':[{'name':'other2'}],'meta':{'total':2},'links':{}}]
        with patch.object(a,'absence_http',side_effect=pages):a.absence_inventory('digitalocean','droplets','droplets',{'demo-app'},'token')
        with patch.object(a,'absence_http',return_value={'droplets':[],'meta':{'total':0},'links':{}}):a.absence_inventory('digitalocean','droplets','droplets',{'demo-app'},'token')
        for mutate in (lambda p:p['meta'].update(total=3),lambda p:p['links']['pages'].update(next='https://evil.test/droplets?page=2'),lambda p:p['links']['pages'].update(next='https://api.digitalocean.com/v2/droplets?name=other'),lambda p:p.update(droplets=[{'name':'demo-app'}]),lambda p:p.pop('meta')):
            broken=copy.deepcopy(pages);mutate(broken[0])
            with patch.object(a,'absence_http',side_effect=broken),self.assertRaises(a.ResourceError):a.absence_inventory('digitalocean','droplets','droplets',{'demo-app'},'token')

    def test_hcloud_and_vultr(self):
        data={'servers':[],'meta':{'pagination':{'page':1,'next_page':None,'total_entries':0}}}
        with patch.object(a,'absence_http',return_value=data):a.absence_inventory('hcloud','servers','servers',{'demo-app'},'token')
        for pagination in ({}, {'page':1,'next_page':None,'total_entries':1},{'page':1,'next_page':1,'total_entries':0}):
            with patch.object(a,'absence_http',return_value=dict(data,meta={'pagination':pagination})),self.assertRaises(a.ResourceError):a.absence_inventory('hcloud','servers','servers',{'demo-app'},'token')
        data={'instances':[],'meta':{'total':0,'links':{'next':'','prev':''}}}
        with patch.object(a,'absence_http',return_value=data):a.absence_inventory('vultr','instances','instances',{'demo-app'},'token')
        data['instances']=[{'label':'demo-app'}];data['meta']['total']=1
        with patch.object(a,'absence_http',return_value=data),self.assertRaises(a.ResourceError):a.absence_inventory('vultr','instances','instances',{'demo-app'},'token')

    def test_provider_credentials_and_registration_checks(self):
        for provider,binding in (('digitalocean','DO_TOKEN'),('hcloud','HCLOUD_TOKEN'),('vultr','VULTR_API_KEY')):
            opts=dict(self.opts,**{'provider-compute':provider})
            with patch.dict(os.environ,{},clear=True),self.assertRaises(a.ResourceError):a.absence_provider(opts,{'demo-app'},{'demo-registration-access'})
            with patch.dict(os.environ,{'COLORS_PAR_'+binding:'token'},clear=True),patch.object(a,'absence_inventory') as inventory:
                a.absence_provider(opts,{'demo-app'},{'demo-registration-access'})
                self.assertEqual(inventory.call_count,2)
                self.assertEqual(inventory.call_args.args[-2],{'demo-registration-access'})

    def test_aws_names_keys_and_incomplete_pages(self):
        opts=dict(self.opts,**{'provider-compute':'aws','aws-region':'us-east-1'})
        pages=[{'Reservations':[]},{'KeyPairs':[]}]
        with patch.object(a,'run',side_effect=[(0,json.dumps(p).encode(),b'') for p in pages]):a.absence_provider(opts,{'demo-app'},{'demo-registration-access'})
        for instance in ({'InstanceId':'i-1','Tags':[{'Key':'Name','Value':'demo-app'}]}, {'InstanceId':'i-1','KeyName':'demo-registration-access'}):
            with patch.object(a,'run',return_value=(0,json.dumps({'Reservations':[{'Instances':[instance]}]}).encode(),b'')),self.assertRaises(a.ResourceError):a.absence_provider(opts,{'demo-app'},{'demo-registration-access'})
        for data in ({'Reservations':[],'NextToken':'more'}, {'Reservations':[{}]}, {}):
            with patch.object(a,'run',return_value=(0,json.dumps(data).encode(),b'')),self.assertRaises(a.ResourceError):a.absence_provider(opts,{'demo-app'},set())

if __name__ == '__main__':unittest.main()
