"""Provider inventory scope and refusal tests for the shared SSH verifier."""
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('absence_adapter', Path(__file__).parents[2] / 'agents' / 'ssh-resource.py')
# tests lives under blue/tests, repository root is parents[2].
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def inventory(monkeypatch, value):
    calls = []
    def run(args, env, **kw):
        calls.append((args, env))
        return 0, json.dumps(value).encode(), b''
    monkeypatch.setattr(adapter, 'run', run)
    return calls


def test_azure_explicit_subscription_unfiltered_inventory(monkeypatch):
    monkeypatch.setenv('AZURE_CONFIG_DIR', '/tmp/azure-fixture')
    calls = inventory(monkeypatch, [])
    adapter.absence_other_provider({'provider-compute':'azure','azure-subscription-id':'sub'}, {'app-node'}, set())
    args, env = calls[0]
    assert args == ['az','vm','list','--subscription','sub','--output','json','--only-show-errors']
    assert env['AZURE_CONFIG_DIR'] == '/tmp/azure-fixture'


@pytest.mark.parametrize('value', [None, {}, [{'name':'app-node','id':'vm'}], [{'name':'other'}]])
def test_azure_refuses_existing_or_malformed(monkeypatch, value):
    inventory(monkeypatch, value)
    with pytest.raises(adapter.ResourceError):
        adapter.absence_other_provider({'provider-compute':'azure','azure-subscription-id':'sub'}, {'app-node'}, set())


def test_oci_config_profile_and_all_pages(monkeypatch):
    monkeypatch.setenv('HOME','/tmp/home-fixture')
    calls = inventory(monkeypatch, {'data':[]})
    adapter.absence_other_provider({'provider-compute':'oci','oci-compartment-id':'comp','oci-config-file-profile':'CUSTOM'}, {'app-node'}, set())
    args, env = calls[0]
    assert '--all' in args
    assert args[args.index('--profile')+1] == 'CUSTOM'
    assert args[args.index('--config-file')+1] == '/tmp/home-fixture/.oci/config'
    assert args[args.index('--auth')+1] == 'api_key'


@pytest.mark.parametrize('value', [{}, {'data':None}, {'data':[],'opc-next-page':'next'}, {'data':[{'id':'vm','display-name':'app-node','lifecycle-state':'TERMINATED'}]}])
def test_oci_refuses_existing_and_incomplete(monkeypatch,value):
    inventory(monkeypatch,value)
    with pytest.raises(adapter.ResourceError):
        adapter.absence_other_provider({'provider-compute':'oci','oci-compartment-id':'comp','oci-config-file-profile':'CUSTOM'}, {'app-node'},set())


def test_yandex_uses_explicit_token_scope_and_all_pages(monkeypatch):
    monkeypatch.setenv('COLORS_PAR_YANDEX_TOKEN','t1.fixture')
    pages = [{'instances':[{'id':'one','name':'foreign'}], 'nextPageToken':'next'}, {'instances':[]}]
    calls=[]
    def http(url,token):
        calls.append((url,token))
        return pages.pop(0)
    monkeypatch.setattr(adapter,'absence_http',http)
    adapter.absence_other_provider({'provider-compute':'yandex','yandex-folder-id':'folder'}, {'app-node'},set())
    assert len(calls)==2
    assert all('folderId=folder' in url and token=='t1.fixture' for url,token in calls)
    assert 'pageToken=next' in calls[1][0]


@pytest.mark.parametrize('value', [{'instances':[{'id':'one','name':'app-node'}]}, {'instances':None}, {'instances':[],'nextPageToken':3}, {'instances':[],'nextPageToken':'next'}, {'error':{'code':403}}])
def test_yandex_refuses_existing_and_incomplete(monkeypatch,value):
    monkeypatch.setenv('COLORS_PAR_YANDEX_TOKEN','t1.fixture')
    monkeypatch.setattr(adapter,'absence_http',lambda *args:value)
    with pytest.raises(adapter.ResourceError):
        adapter.absence_other_provider({'provider-compute':'yandex','yandex-folder-id':'folder'}, {'app-node'},set())


def test_yandex_oauth_exchange_uses_request_body_not_argv(monkeypatch):
    import urllib.request
    captured=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,limit): return b'{"iamToken":"t1.exchanged"}'
    class Opener:
        def open(self,request,timeout):
            captured.append(request)
            return Response()
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args:Opener())
    assert adapter.absence_yandex_token('oauth-fixture')=='t1.exchanged'
    request=captured[0]
    assert request.full_url=='https://iam.api.cloud.yandex.net/iam/v1/tokens'
    assert json.loads(request.data)=={'yandexPassportOauthToken':'oauth-fixture'}
