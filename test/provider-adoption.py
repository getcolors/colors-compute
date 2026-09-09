#!/usr/bin/env python3
"""Prove an unchanged existing cluster consumer adopts an isolated library provider.

No production registry, application file, environment credential or cloud resource
is changed. Fixturecloud is an in-memory test adapter cloned from reviewed Vultr
rendering data; it is not an advertised cloud implementation.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT.parent / 'automq'
PROVIDER = 'fixturecloud'


def digest(paths):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for directory in paths for path in sorted(directory.rglob('*'))
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}


def main():
    sources = [APP/'red/src', APP/'blue/src', APP/'green/src']
    before = digest(sources)
    fixture = subprocess.run([os.environ.get('BUN','bun'),'-e',"console.log(JSON.stringify(Bun.YAML.parse(await Bun.file(process.argv[1]).text())))",str(APP/'test/fixtures/colors.yml')],capture_output=True,text=True,check=True)
    opts = {**json.loads(fixture.stdout),'profile':'adoption', 'provider-compute':PROVIDER, 'provider-backend':'s3', 's3-bucket':'states', 's3-region':'us-east-1',
            'automq-node-count':3, 'automq-host':'example.invalid', 'automq-ssh-sources':['192.0.2.0/24'],
            'automq-kafka-sources':[], 'vultr-region':'ewr', 'vultr-plan':'vc2-2c-4gb', 'vultr-os-id':2284,
            'vultr-vpc-subnet':'10.40.0.0/24'}
    with tempfile.TemporaryDirectory(prefix='colors-provider-adoption-') as directory:
        temp = Path(directory); lib = temp/'library'
        for relative in ['red/src','red/resources','blue/src','green/src']:
            shutil.copytree(ROOT/relative,lib/relative,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copy2(ROOT/'package.json',lib/'package.json')
        (lib/'red/node_modules').symlink_to(ROOT/'red/node_modules',target_is_directory=True)
        consumer=temp/'consumer';consumer.mkdir();shutil.copytree(APP/'red/src',consumer/'src');shutil.copytree(APP/'red/resources',consumer/'resources')
        (consumer/'node_modules').mkdir()
        for dependency in (APP/'red/node_modules').iterdir():
            (consumer/'node_modules'/dependency.name).symlink_to(lib if dependency.name=='colors-compute-red' else dependency.resolve(),target_is_directory=True)
        (temp/'opts.json').write_text(json.dumps(opts))
        (consumer/'run.ts').write_text("""import * as c from './src/cluster.ts';import {automqWorkflow} from './src/workflow.ts';import {run} from 'red/workflow';import {plan_deployment,validate} from 'colors-compute-red';import {readFileSync} from 'node:fs';
const opts={...JSON.parse(readFileSync(process.argv[2],'utf8')),'red/event':'build',workdir:process.argv[3]};try{const result=await run(automqWorkflow,opts);if(result['red/exit']!==0)throw Error('build refused');const plan=plan_deployment(opts,c.topology(opts),c.requirements(opts));console.log('ADOPTION:'+JSON.stringify({nodes:c.fallbackNodes(opts),names:c.machineNames(opts),documents:plan.documents,validation:validate(opts),build_exit:result['red/exit']}));}catch{console.log('ADOPTION:'+JSON.stringify({status:'unsupported'}));}
""")
        (temp/'run.py').write_text("""import asyncio,json,sys
from package_automq_blue import cluster as c,workflow
from blue.workflow import run
from colors_compute import validate
from colors_compute.planning import plan_deployment
o={**json.load(open(sys.argv[1])),'blue/event':'build','workdir':sys.argv[2]}
async def main():
 try:
  result=await run(workflow.automq_workflow,o)
  if result.get('blue/exit')!=0:raise ValueError('build refused')
  plan=plan_deployment(o,c.topology(o),c.requirements(o))
  print('ADOPTION:'+json.dumps({'nodes':c.fallback_nodes(o),'names':c.machine_names(o),'documents':plan['documents'],'validation':validate(o),'build_exit':result['blue/exit']}))
 except Exception:print('ADOPTION:'+json.dumps({'status':'unsupported'}))
asyncio.run(main())
""")
        (temp/'run.clj').write_text("""(require '[cheshire.core :as j] '[io.github.getcolors.automq.cluster :as c] '[io.github.getcolors.automq.workflow :as w] '[green.workflow :as wf] '[io.github.getcolors.compute :as compute] '[io.github.getcolors.compute-planning :as planning])
(let [opts (assoc (j/parse-string (slurp (first *command-line-args*)) true) :green/event :build :workdir (second *command-line-args*))]
 (println (str "ADOPTION:" (j/generate-string (try (let [result (wf/run w/workflow opts) _ (when-not (= 0 (:green/exit result)) (throw (ex-info "build refused" {}))) plan (planning/plan-deployment opts (c/topology opts) (c/requirements opts))] {:nodes (c/fallback-nodes opts) :names (c/machine-names opts) :documents (:documents plan) :validation (compute/validate opts) :build_exit (:green/exit result)}) (catch Exception _ {:status "unsupported"}))))))
""")
        green_cp=subprocess.run([os.environ.get('BB','bb'),'-e',"(require '[babashka.classpath :as cp]) (print (cp/get-classpath))"],cwd=APP/'green',capture_output=True,text=True,check=True).stdout
        commands = {
            'red':[os.environ.get('BUN','bun'),str(consumer/'run.ts'),str(temp/'opts.json'),str(temp/'build-red')],
            'blue':[str(APP/'blue/.venv/bin/python'),str(temp/'run.py'),str(temp/'opts.json'),str(temp/'build-blue')],
            'green':[os.environ.get('BB','bb'),'--classpath',os.pathsep.join(map(str,[lib/'green/src/clj',lib/'green/src/resources',Path(green_cp)])),str(temp/'run.clj'),str(temp/'opts.json'),str(temp/'build-green')],
        }
        env={k:v for k,v in os.environ.items() if not k.startswith(('COLORS_PAR_','TF_','TOFU_'))}
        env.update(PYTHONPATH=os.pathsep.join(map(str,[lib/'blue/src',APP/'blue/src'])),PYTHONDONTWRITEBYTECODE='1')
        def run(color):
            result=subprocess.run(commands[color],cwd=temp,env=env,capture_output=True,text=True,check=True)
            return json.loads(next(line[len('ADOPTION:'):] for line in reversed(result.stdout.splitlines()) if line.startswith('ADOPTION:')))
        for color in commands:
            assert run(color)=={'status':'unsupported'}, f'{color}: baseline already accepts fixture provider'
        # Only the dependency's packaged registry, recipe and rendering data change.
        for resource in [lib/'red/resources',lib/'blue/src/colors_compute',lib/'green/src/resources/colors_compute']:
            registry=json.loads((resource/'providers.json').read_text());registry['compute'][PROVIDER]=deepcopy(registry['compute']['vultr'])
            recipes=json.loads((resource/'provider-recipes.json').read_text());recipes[PROVIDER]=deepcopy(recipes['vultr']);recipes[PROVIDER]['planning_shared']['params']['provider']=PROVIDER
            templates=json.loads((resource/'templates.json').read_text());templates[PROVIDER]=deepcopy(templates['vultr'])
            for stage in templates[PROVIDER].values():
                for document in stage.values():
                    params=document.get('output',{}).get('params',{}).get('value')
                    if isinstance(params,dict) and params.get('provider')=='vultr':params['provider']=PROVIDER
            for name,data in [('providers.json',registry),('provider-recipes.json',recipes),('templates.json',templates)]:
                (resource/name).write_text(json.dumps(data))
        outputs={color:run(color) for color in commands}
        for color,result in outputs.items():
            assert result.get('build_exit')==0 and result.get('validation')==[], (color,result)
            assert len(result.get('documents',{}).get('nodes',{}))==3 and result['documents']['shared'], (color,result)
            assert list((temp/('build-'+color)).rglob('inventory.json')) and list((temp/('build-'+color)).rglob('compose.yml')), color
            assert len(result.get('nodes',[]))==3, (color,result)
            assert all(n['provider']==PROVIDER for n in result['nodes']), (color,result)
            assert result['names']==['adoption-0','adoption-1','adoption-2'], (color,result)
            assert all(n['vpc-ip'].startswith('10.40.0.') for n in result['nodes'])
            print(f'{color}: unchanged AutoMQ workflow built compute, DNS and Ansible using dependency-only fixture provider')
        assert outputs['red']==outputs['blue']==outputs['green'], 'consumer results disagree across languages'
        assert (consumer/'src/cluster.ts').read_bytes()==(APP/'red/src/cluster.ts').read_bytes()
    assert digest(sources)==before, 'consumer source changed during proof'
    print('Production bundles unchanged; fixture adapter was confined to a deleted temporary dependency copy.')


if __name__=='__main__':main()
