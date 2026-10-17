"""Guarded integration fixture: real Lean and all node-finish gates, no research credit.

Run via lockstep job run, passing an output directory. Uses a private project/database.
"""
from pathlib import Path
import json
import os
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    from lockstep.config import load_config
    from lockstep.context import Ctx
    from lockstep.commands.node import run_add
    from lockstep.commands.search import run as search
    from lockstep.commands.extract import run_extract
    from lockstep.commands.claims import run_claim
    from lockstep.finalize import submit
    from lockstep.operations import show
    from lockstep.jobs import run_job
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True,exist_ok=True)
    (root/'harness').mkdir(exist_ok=True)
    (root/'lean-toolchain').write_text((load_config().root/'lean-toolchain').read_text())
    (root/'lakefile.lean').write_text('import Lake\nopen Lake DSL\npackage smoke\nlean_lib TCore\nlean_lib TBlueprint\n')
    (root/'TCore.lean').write_text('import Lean\n')
    (root/'TBlueprint.lean').write_text('import TCore\n')
    (root/'harness/config.toml').write_text('''[project]
id = "automation-smoke"
state_home = ".state"
blueprint_namespace = "TBlueprint"
core_namespace = "TCore"
workstreams = ["A"]
[layers]
order = ["TCore", "TBlueprint"]
core = "TCore"
blueprint = "TBlueprint"
[layers.TCore]
prefixes = ["TCore"]
[layers.TBlueprint]
prefixes = ["TBlueprint"]
exclude_from_root = ["TBlueprint.Extract"]
[audit]
standard_axioms = ["propext", "Classical.choice", "Quot.sound"]
[build]
memory_max = "2G"
guard = true
[build.targets]
blueprint = ["TBlueprint"]
core = ["TCore"]
[extract]
memory_max = "2G"
targeted_memory_max = "2G"
budget_min = 5
[jobs]
memory_budget = "4G"
''')
    for file,text in [('statements.lock','{}'),('allowlist.json','[]'),('destinations.txt','')]:
        (root/'harness'/file).write_text(text)
    # The enclosing lockstep job supplies the physical cgroup. This isolated fixture
    # has its own operational database and cannot overwrite the research index.
    os.environ.pop('LOCKSTEP_JOB_ID',None)
    os.environ.pop('LOCKSTEP_HOME',None)
    os.environ['LOCKSTEP_PROJECT_ROOT']=str(root)
    os.environ['LOCKSTEP_IN_GUARDED_STAGE']='1'
    os.environ['LOCKSTEP_STAGE_MEMORY']='4G'
    cfg=load_config(root)
    ctx=Ctx(cfg,agent="smoke:automation")
    for args in [('init','-q','-b','node/Smoke'),('add','.'),('-c','user.email=smoke@local','-c','user.name=Smoke','commit','-qm','fixture')]:
        subprocess.run(['git','-C',str(root),*args],check=True)
    search(ctx,query='reflexivity of natural number equality')
    added=run_add(ctx,'smoke','A','formalization',': ∀ n : Nat, n = n','Automation fixture only.',dangling_ok='isolated harness integration fixture')
    if added.exit_code: raise RuntimeError(added.payload)
    run_extract(ctx,force=True)
    run_claim(ctx,'smoke',why='known proof for automation integration',force=True)
    body=root/'body.lean.txt';body.write_text('by\n  intro n\n  rfl')
    op=submit(cfg,ctx.agent,'smoke',body)
    for _ in range(8):
        current=show(cfg,op['id'])
        if current['status']!='running': break
        stage=next(s for s in current['stages'] if s['status']=='running')
        run_job(cfg,stage['job_id'])
    final=show(cfg,op['id'])
    (root/'result.json').write_text(json.dumps(final,indent=2))
    print(json.dumps({'id':final['id'],'status':final['status'],'stages':[(s['name'],s['status']) for s in final['stages']]}))
    if final['status']=='succeeded':
        from lockstep import proofs
        from lockstep.artifacts import put
        fixture=proofs.capture(cfg,'smoke')['id']
        native=proofs.prove(cfg,fixture,tactics=['rfl','simp'],memory='2G',minutes=1)
        bad=proofs.check(cfg,fixture,put(cfg,b'by\n  skip','text/x-lean')['id'],memory='2G',minutes=1)
        (root/'native-and-negative.json').write_text(json.dumps({'native':native,'unresolved':bad},indent=2))
        if not native['proof_accepted'] or bad['proof_accepted']:
            raise RuntimeError('native route / unresolved-goal rejection failed')
    return 0 if final['status']=='succeeded' else 1


if __name__=='__main__': raise SystemExit(main())
