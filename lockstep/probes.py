"""Generic plugin admission, versioned finite results and independent witness replay."""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

from lockstep.artifacts import canonical, put_json, read
from lockstep.errors import UsageError


def plugin(cfg, name):
    config = cfg.get('research','plugins',name)
    if not isinstance(config, dict) or not config.get('module'):
        raise UsageError('probe plugin is not registered in project configuration')
    module = config['module']
    sources = config.get('sources', [module.replace('.','/')+'.py'])
    versions = {}
    for rel in sources:
        path = (cfg.root / rel).resolve()
        if not path.is_relative_to(cfg.root.resolve()) or not path.is_file():
            raise UsageError('plugin sources must be existing project files')
        versions[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return config, versions


def submit(cfg, agent, spec):
    from lockstep.operations import submit as operation
    if spec.get('schema_version') != 1:
        raise UsageError('probe schema_version must be 1')
    config, versions = plugin(cfg, spec.get('plugin'))
    if spec.get('semantics') not in config.get('semantics', []):
        raise UsageError('explicit semantics is not registered for this plugin')
    budget = spec.get('budget', {})
    if type(budget.get('cases')) is not int or budget['cases'] <= 0 or not isinstance(budget.get('seconds'), (int,float)) or budget['seconds'] <= 0:
        raise UsageError('positive case and seconds limits required')
    frozen = {'spec':spec, 'module':config['module'], 'sources':versions}
    ref = put_json(cfg, frozen, 'probe-request-v1')
    return operation(cfg, agent, {'schema_version':1,'kind':'probe','stages':[{'name':'finite-probe','action':'probe','request':ref['id'],
                     'minutes':budget['seconds']/60+1,'memory':budget.get('memory','2G'),'lock':'probe:'+ref['id']}]})


def load(cfg, frozen):
    config, versions = plugin(cfg, frozen['spec']['plugin'])
    if versions != frozen['sources'] or config['module'] != frozen['module']:
        raise UsageError('plugin changed after admission')
    if str(cfg.root) not in sys.path: sys.path.insert(0,str(cfg.root))
    return importlib.import_module(config['module'])


def execute(cfg, stage):
    frozen = json.loads(read(cfg, stage['request']))
    module = load(cfg, frozen)
    value = module.evaluate(frozen['spec'])
    if value.get('witness') is not None:
        replay = module.replay(value['witness'])
        if replay.get('replayed') is not True:
            return {'ok':False,'outcome':'witness_replay_failed','proof_accepted':False}
        value['replay'] = replay
    value.update(ok=True, proof_accepted=False, request=stage['request'], semantic_version=frozen['spec']['semantics'], source_hashes=frozen['sources'])
    value['result_artifact'] = put_json(cfg,value,'probe-result-v1')
    return value


def replay(cfg, ident):
    result = json.loads(read(cfg,ident))
    frozen = json.loads(read(cfg,result['request']))
    if result.get('witness') is None:
        raise UsageError('result has no witness to replay')
    return load(cfg,frozen).replay(result['witness'])
