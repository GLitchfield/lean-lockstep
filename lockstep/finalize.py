"""Prepared proof -> existing checks -> reviewable change, with no implicit publication."""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

from lockstep import proofs
from lockstep.artifacts import put, put_json, read
from lockstep.context import Ctx
from lockstep.errors import UsageError
from lockstep.gitutil import git, head_sha
from lockstep.store import Store


def preflight(cfg, agent, f):
    proofs.validate(cfg, f)
    expected_branch = 'node/' + f['module'].split('.')[-1]
    if git(cfg, 'branch', '--show-current').strip() != expected_branch:
        raise UsageError(f'node finish requires branch {expected_branch}')
    with Store(cfg) as st:
        claim = st.claims().get(f['parent'])
    if not claim or claim['agent'] != agent:
        raise UsageError('node finish requires a live claim owned by this agent')
    lock = json.loads(cfg.lock_file.read_text())
    if lock.get(f['parent'], {}).get('type_hash') != f['type_hash']:
        raise UsageError('registered statement lock does not match fixture')
    return {'branch': expected_branch, 'base': head_sha(cfg), 'claim': claim}


def submit(cfg, agent, name, body_path, *, fixture_id=None, policy='review', extract_memory=None):
    from lockstep.operations import submit as operation
    from lockstep.commands.search import run as search
    if policy != 'review':
        raise UsageError('only review policy is enabled; publication requires a separately configured adapter')
    fixture_id = fixture_id or proofs.capture(cfg, name)['id']
    f = proofs.fixture(cfg, fixture_id)
    from lockstep.nodes import find_node
    if find_node(cfg,name).name != f['parent']:
        raise UsageError('fixture parent does not match the requested node')
    identity = preflight(cfg, agent, f)
    body = put(cfg, Path(body_path).read_bytes(), 'text/x-lean')
    search_receipt = put_json(cfg, search(Ctx(cfg, agent=agent), query=name).payload, 'search-receipt-v1')
    common = {'fixture': fixture_id, 'body': body['id'], 'identity': identity, 'search': search_receipt}
    stages = [
        {'name':'candidate', 'action':'proof_check', 'memory':cfg.build_memory_max, 'minutes':20},
        {'name':'original-source', 'action':'node_check', 'memory':max((cfg.build_memory_max, cfg.extract_targeted_memory_max), key=lambda m: __import__('lockstep.build', fromlist=['_mem_gib'])._mem_gib(m)), 'minutes':30},
        {'name':'full-extract', 'action':'node_extract', 'memory':extract_memory or cfg.extract_memory_max, 'minutes':cfg.extract_budget_min},
        {'name':'audits', 'action':'node_audit', 'memory':'2G', 'minutes':10},
        {'name':'review', 'action':'node_prepare', 'memory':'2G', 'minutes':5},
    ]
    for stage in stages: stage.update(common)
    return operation(cfg, agent, {'schema_version':1, 'kind':'node_finish', 'policy':'review', 'stages':stages}, node=f['parent'])


def run(cfg, op, stage):
    f = proofs.fixture(cfg, stage['fixture'])
    source = proofs.assembled(cfg, f, read(cfg, stage['body']))
    ctx = Ctx(cfg, agent=op['agent'])
    action = stage['action']
    if action == 'node_check':
        # On a deliberate retry, reuse the exact already-inserted source. A crash
        # with unknown execution is never automatically retried by operations.
        path = cfg.root / f['file']
        if path.read_bytes() != source:
            preflight(cfg, op['agent'], f)
            prior = op['stages'][0]['result']
            if prior.get('proof_accepted') is not True or prior.get('body') != stage['body']:
                raise UsageError('missing accepted candidate receipt')
            temp = path.with_suffix('.lean.lockstep-new')
            temp.write_bytes(source)
            temp.replace(path)
        from lockstep.commands.check import run as check
        checked = check(ctx, f['parent'], budget=int(stage['minutes']), targeted=True, force=True)
        node = (checked.payload or {}).get('node', {})
        accepted = checked.exit_code == 0 and isinstance(node, dict) and node.get('status') == 'closed' and not node.get('sorry') and node.get('type_hash') == f['type_hash'] and set(node.get('axioms', [])) <= set(f['standard_axioms'])
        return {'ok': accepted, 'proof_accepted': accepted, 'targeted': True, 'check': checked.payload}
    if (cfg.root / f['file']).read_bytes() != source:
        raise UsageError('candidate source changed between workflow stages')
    if git(cfg, 'branch', '--show-current').strip() != stage['identity']['branch'] or head_sha(cfg) != stage['identity']['base']:
        raise UsageError('branch or HEAD changed during finalization')
    if action == 'node_extract':
        from lockstep.commands.extract import run_extract
        payload, index, problems = run_extract(ctx, no_cache=True, force=True, memory_max=stage['memory'])
        return {'ok': not problems, 'frontier_fresh': not problems, 'problems': problems, 'tree_hash': payload.get('tree_hash'), 'nodes':len(payload['nodes']), 'index':len(index)}
    if action == 'node_audit':
        from lockstep.commands.audit import run as audit
        from lockstep.commands.layers import run as layers
        results = [audit(ctx), layers(ctx)]
        return {'ok': all(r.exit_code == 0 for r in results), 'checks':[r.payload for r in results]}
    if action == 'node_prepare':
        original = read(cfg, f['source']['id']).decode()
        diff = ''.join(difflib.unified_diff(original.splitlines(True), source.decode().splitlines(True), fromfile='a/'+f['file'], tofile='b/'+f['file']))
        receipt = {'ok':True, 'proof_accepted':True, 'frontier_fresh':True, 'published':False, 'policy':'review', 'parent':f['parent'],
                   'diff':put(cfg, diff.encode(), 'text/x-diff'), 'source_hash':hashlib.sha256(source).hexdigest(), 'files':[f['file']], 'search':stage['search']}
        with Store(cfg) as st:
            claim = st.claims().get(f['parent'])
            if claim and claim['agent'] == op['agent']:
                st.release(f['parent'], op['agent'], 'closed', 'node finish: original source checked, full extraction and audits passed; review artifact prepared')
        return receipt
    raise UsageError(f'unsupported finalization action {action}')
