"""Durable, finite workflows on the existing job scheduler.

No external effect is retried automatically. A dead runner is unknown execution,
not permission to replay its command. Mathematical acceptance lives in receipts.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from lockstep.artifacts import canonical, put_json
from lockstep.build import _mem_gib, run_guarded
from lockstep.config import load_config
from lockstep.errors import UsageError
from lockstep.store import Store

TERMINAL = {'succeeded', 'failed', 'cancelled', 'needs_agent', 'blocked_resource', 'unknown_execution'}
ACTIONS = {'command', 'proof_check', 'proof_prove', 'node_check', 'node_audit', 'node_extract', 'node_prepare', 'probe'}


def show(cfg, ident):
    with Store(cfg) as st:
        row = st.conn.execute('SELECT * FROM operations WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise UsageError(f'unknown operation {ident}')
        op = dict(row)
        op['spec'] = json.loads(op.pop('spec_json'))
        op['result'] = json.loads(op.pop('result_json') or '{}')
        op['stages'] = [dict(r) for r in st.conn.execute('SELECT * FROM operation_stages WHERE operation_id=? ORDER BY ordinal', (ident,))]
        for stage in op['stages']:
            stage['result'] = json.loads(stage.pop('result_json') or '{}')
        return op


def submit(cfg, agent, spec, *, key=None, node=None):
    if not cfg.get('automation','enabled',default=True):
        raise UsageError('new automation admissions disabled; existing operations remain readable and cancellable')
    if spec.get('schema_version') != 1 or not isinstance(spec.get('stages'), list) or not spec['stages']:
        raise UsageError('expected schema_version 1 and nonempty stages')
    spec = json.loads(canonical(spec))
    names = set()
    for stage in spec['stages']:
        name = stage.get('name')
        if not isinstance(name, str) or not name or name in names or stage.get('action') not in ACTIONS:
            raise UsageError('stage names must be unique; action must be registered')
        names.add(name)
        stage.setdefault('memory', cfg.jobs_default_memory_max)
        stage.setdefault('minutes', 10)
        if _mem_gib(stage['memory']) <= 0 or not isinstance(stage['minutes'], (int, float)) or stage['minutes'] <= 0:
            raise UsageError('positive stage memory and time limits required')
        if stage['action'] == 'command':
            if not isinstance(stage.get('argv'), list) or not stage['argv'] or not all(isinstance(a, str) for a in stage['argv']):
                raise UsageError('command stage requires a nonempty argv string array')
        stage.setdefault('lock', 'worktree:' + str(cfg.root.resolve()))
    identity = {'root': str(cfg.root.resolve()), 'agent': agent, 'spec': spec, 'node': node}
    digest = hashlib.sha256(canonical(identity)).hexdigest()
    operation_key = str(cfg.root.resolve()) + ':' + agent + ':' + (key or digest)
    ident = 'op-' + uuid.uuid4().hex[:16]
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        old = st.conn.execute('SELECT id,spec_json,node FROM operations WHERE operation_key=?', (operation_key,)).fetchone()
        if old:
            if json.loads(old['spec_json']) != spec or old['node']!=node:
                raise UsageError('operation key already refers to different inputs')
            ident = old['id']
        else:
            at = time.time()
            st.conn.execute('INSERT INTO operations(id,operation_key,kind,agent,root,node,spec_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (ident, operation_key, spec.get('kind', 'workflow'), agent, str(cfg.root.resolve()), node, canonical(spec).decode(), 'pending', at, at))
            for i, stage in enumerate(spec['stages']):
                st.conn.execute('INSERT INTO operation_stages(operation_id,ordinal,name,status,input_hash) VALUES (?,?,?,?,?)',
                                (ident, i, stage['name'], 'pending', hashlib.sha256(canonical(stage)).hexdigest()))
    advance(cfg, ident)
    return show(cfg, ident)


def _terminal(st, op, status, result):
    at = time.time()
    payload = {'schema_version': 1, 'operation_id': op['id'], 'status': status, **result}
    st.conn.execute('UPDATE operations SET status=?,result_json=?,updated_at=? WHERE id=?', (status, canonical(payload).decode(), at, op['id']))
    # Each attempt has a stable event ID, so controller restarts cannot duplicate it.
    attempt = st.conn.execute('SELECT SUM(attempt) FROM operation_stages WHERE operation_id=?', (op['id'],)).fetchone()[0]
    event_id = f"{op['id']}:{attempt}:{status}"
    st.conn.execute('INSERT OR IGNORE INTO deliveries(id,operation_id,agent,payload_json,status,created_at) VALUES (?,?,?,?,?,?)',
                    (event_id, op['id'], op['agent'], canonical(payload).decode(), 'pending', at))
    if op['kind']=='node_finish' and status!='succeeded':
        claim=st.conn.execute('SELECT agent FROM claims WHERE node=?',(op['node'],)).fetchone()
        if claim and claim['agent']==op['agent']:
            outcome='blocked' if status in ('blocked_resource','unknown_execution') else 'partial'
            note=f'operation {op["id"]}: {status}; successful stage receipts retained'
            st.conn.execute('DELETE FROM claims WHERE node=? AND agent=?',(op['node'],op['agent']))
            st.conn.execute('UPDATE attempts SET ended_at=?,outcome=?,note=? WHERE node=? AND agent=? AND ended_at IS NULL',(at,outcome,note,op['node'],op['agent']))
            st.conn.execute('INSERT INTO events(at,kind,node,agent,detail) VALUES (?,?,?,?,?)',(at,'release',op['node'],op['agent'],canonical({'outcome':outcome,'note':note}).decode()))


def advance(cfg, ident=None):
    """Short controller tick: one transaction queues the next job or emits completion."""
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        query = "SELECT * FROM operations WHERE status IN ('pending','running','cancel_requested')"
        ops = st.conn.execute(query + (' AND id=?' if ident else ''), (ident,) if ident else ()).fetchall()
        for op in ops:
            stages = st.conn.execute('SELECT * FROM operation_stages WHERE operation_id=? ORDER BY ordinal', (op['id'],)).fetchall()
            spec = json.loads(op['spec_json'])
            if op['kind'] == 'node_finish' and time.time()-op['created_at'] < 86400:
                # Renewal is bounded and ownership cannot transfer to this operation.
                st.conn.execute('UPDATE claims SET expires_at=MAX(expires_at,?) WHERE node=? AND agent=?',
                                (min(time.time()+3600,op['created_at']+86400),op['node'],op['agent']))
            current = next((s for s in stages if s['status'] != 'succeeded'), None)
            if current and current['status'] == 'running':
                job = st.conn.execute('SELECT * FROM jobs WHERE id=?', (current['job_id'],)).fetchone()
                if job and job['status'] in ('failed', 'killed', 'unknown_execution'):
                    status = 'cancelled' if op['cancel_requested'] and job['status'] == 'killed' else ('unknown_execution' if job['status'] == 'unknown_execution' else 'failed')
                    st.conn.execute('UPDATE operation_stages SET status=?,finished_at=? WHERE operation_id=? AND ordinal=?', (status, time.time(), op['id'], current['ordinal']))
                    _terminal(st, op, status, {'stage': current['name'], 'job_id': current['job_id'], 'proof_accepted': False,
                                               'termination_acknowledged': status != 'unknown_execution'})
                continue
            if current and current['status'] == 'unknown_execution':
                job = st.conn.execute('SELECT * FROM jobs WHERE id=?', (current['job_id'],)).fetchone()
                try:
                    job_result = json.loads(job['result_json'] or '{}') if job else {}
                except json.JSONDecodeError:
                    job_result = {}
                stopped = (job and job['status'] in ('done', 'failed', 'killed') and isinstance(job_result, dict)
                           and (job_result.get('termination_acknowledged') is True
                                or (job_result.get('termination_acknowledged') is None
                                    and job_result.get('execution_reconciled') == 'scope_stopped')))
                if op['cancel_requested'] and stopped:
                    result = json.loads(op['result_json'] or '{}')
                    for key in ('schema_version', 'operation_id', 'status'):
                        result.pop(key, None)
                    result.update(termination_acknowledged=True)
                    for key in ('execution_reconciled', 'effects'):
                        if key in job_result:
                            result[key] = job_result[key]
                    st.conn.execute("UPDATE operation_stages SET status='cancelled',finished_at=? WHERE operation_id=? AND ordinal=?",
                                    (time.time(), op['id'], current['ordinal']))
                    _terminal(st, op, 'cancelled', result)
                else:
                    st.conn.execute("UPDATE operations SET status='unknown_execution',updated_at=? WHERE id=?",
                                    (time.time(), op['id']))
                continue
            if op['cancel_requested']:
                active = st.conn.execute("SELECT 1 FROM jobs WHERE operation_id=? AND status IN ('starting','running','unknown_execution')",
                                         (op['id'],)).fetchone()
                if active:
                    continue
                _terminal(st, op, 'cancelled', {'termination_acknowledged': True})
                continue
            if current is None:
                receipts = {s['name']: json.loads(s['result_json'] or '{}') for s in stages}
                _terminal(st, op, 'succeeded', {'receipts': receipts, 'proof_accepted': any(r.get('proof_accepted') is True for r in receipts.values()), 'published': False})
                continue
            stage = spec['stages'][current['ordinal']]
            if _mem_gib(stage['memory']) > _mem_gib(cfg.jobs_memory_budget):
                _terminal(st, op, 'blocked_resource', {'stage': current['name'], 'required_memory': stage['memory'], 'shared_budget': cfg.jobs_memory_budget})
                continue
            job_id = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
            cfg.build_log_dir.mkdir(parents=True, exist_ok=True)
            at = time.time()
            st.conn.execute('INSERT INTO jobs(id,kind,node,argv,cwd,status,created_at,log_path,budget_min,agent,why,memory_max,outputs,lock_key,operation_id,stage_ordinal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (job_id, 'operation', op['node'], '[]', op['root'], 'queued', at, str(cfg.build_log_dir / f'job-{job_id}.log'), stage['minutes'], op['agent'], stage['name'], stage['memory'], '[]', stage['lock'], op['id'], current['ordinal']))
            st.conn.execute("UPDATE operation_stages SET status='running',attempt=attempt+1,job_id=?,started_at=? WHERE operation_id=? AND ordinal=?", (job_id, at, op['id'], current['ordinal']))
            st.conn.execute("UPDATE operations SET status='running',updated_at=? WHERE id=?", (at, op['id']))


def execute_stage(cfg, job):
    cfg = load_config(Path(job['cwd']))
    op = show(cfg, job['operation_id'])
    stage = op['spec']['stages'][job['stage_ordinal']]
    try:
        if stage['action'] == 'command':
            result = run_guarded(cfg, stage['argv'], log_path=Path(job['log_path']), budget_min=stage['minutes'], memory_max=stage['memory'],
                                 force=True, count_built=False, job_id=job['id'], cwd=cfg.root,
                                 env_extra={'LOCKSTEP_AGENT': op['agent'], 'LOCKSTEP_JOB_ID': job['id'], 'LOCKSTEP_OPERATION_ID': op['id'], 'LOCKSTEP_STAGE': stage['name'], 'LOCKSTEP_STAGE_MEMORY': stage['memory'], 'LOCKSTEP_PROJECT_ROOT': str(cfg.root), 'LOCKSTEP_IN_GUARDED_STAGE': '1'})
            receipt = {'command_succeeded': result.rc == 0 and not result.budget_exceeded and not result.refused,
                       'proof_accepted': False, 'rc': result.rc, 'seconds': result.seconds, 'peak_mib': result.peak_mib,
                       'timeout': result.budget_exceeded, 'refused': result.refused}
            status = 'succeeded' if receipt['command_succeeded'] else 'failed'
        else:
            # Execute specialized actions in the same memory-guarded process tree.
            import sys
            request = put_json(cfg, {'operation_id': op['id'], 'stage': job['stage_ordinal']}, 'stage-request-v1')
            out = cfg.root / 'runs' / 'lockstep-operations' / job['id'] / 'receipt.json'
            out.parent.mkdir(parents=True, exist_ok=True)
            result = run_guarded(cfg, [sys.executable, '-m', 'lockstep.stage_runner', request['id'], str(out)], log_path=Path(job['log_path']),
                                 budget_min=stage['minutes'], memory_max=stage['memory'], force=True, count_built=False, job_id=job['id'], cwd=cfg.root,
                                 env_extra={'LOCKSTEP_AGENT': op['agent'], 'LOCKSTEP_JOB_ID': job['id'], 'LOCKSTEP_OPERATION_ID': op['id'], 'LOCKSTEP_STAGE': stage['name'], 'LOCKSTEP_STAGE_MEMORY': stage['memory'], 'LOCKSTEP_PROJECT_ROOT': str(cfg.root), 'LOCKSTEP_IN_GUARDED_STAGE': '1'})
            receipt = json.loads(out.read_text()) if out.exists() else {'error': 'stage exited without a receipt', 'proof_accepted': False}
            receipt.update(seconds=result.seconds, peak_mib=result.peak_mib, timeout=result.budget_exceeded)
            status = 'succeeded' if result.rc == 0 and receipt.get('ok') is True and not result.budget_exceeded else 'failed'
        if not result.termination_acknowledged:
            status = 'unknown_execution'
            receipt['termination_acknowledged'] = False
        artifact = put_json(cfg, receipt, 'stage-receipt-v1')
        receipt['artifact'] = artifact
    except Exception as exc:
        status, receipt = 'failed', {'error': str(exc), 'error_type': type(exc).__name__, 'proof_accepted': False}
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        row = st.conn.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if row['cancel_requested'] and status != 'unknown_execution':
            status = 'cancelled'
            receipt['termination_acknowledged'] = True
        at = time.time()
        st.conn.execute('UPDATE jobs SET status=?,exit_code=?,finished_at=?,result_json=?,seconds=?,peak_mib=? WHERE id=?',
                        ('done' if status == 'succeeded' else ('killed' if status == 'cancelled' else ('unknown_execution' if status=='unknown_execution' else 'failed')), 0 if status == 'succeeded' else 1, at, canonical(receipt).decode(), receipt.get('seconds'), receipt.get('peak_mib'), job['id']))
        st.conn.execute('UPDATE operation_stages SET status=?,finished_at=?,result_json=? WHERE operation_id=? AND ordinal=?', (status, at, canonical(receipt).decode(), op['id'], job['stage_ordinal']))
        if status != 'succeeded':
            _terminal(st, op, status, {'stage': stage['name'], **receipt})
    advance(cfg, op['id'])
    with Store(cfg) as st:
        return st.job(job['id'])


def control(cfg, ident, action):
    op = show(cfg, ident)
    if action == 'cancel':
        with Store(cfg) as st:
            st.conn.execute('BEGIN IMMEDIATE')
            st.conn.execute("UPDATE operations SET cancel_requested=1,status='cancel_requested' "
                            "WHERE id=? AND status IN ('pending','running','cancel_requested','unknown_execution')", (ident,))
        from lockstep.jobs import request_cancel
        for stage in op['stages']:
            if stage['status'] in ('running', 'unknown_execution') and stage['job_id']:
                request_cancel(cfg, stage['job_id'])
        advance(cfg, ident)
    elif action in ('resume', 'retry'):
        if op['status'] == 'unknown_execution':
            raise UsageError('execution is unknown; reconcile process and effects before submitting a new operation')
        if op['status'] in ('failed', 'cancelled', 'needs_agent') and action != 'retry':
            raise UsageError('use explicit retry for a failed/cancelled stage; successful stages are preserved')
        with Store(cfg) as st:
            st.conn.execute('BEGIN IMMEDIATE')
            active = st.conn.execute("SELECT 1 FROM jobs WHERE operation_id=? AND status IN ('queued','starting','running','unknown_execution')", (ident,)).fetchone()
            if active:
                raise UsageError('operation still has live or uncertain execution')
            if op['status'] != 'succeeded':
                st.conn.execute("UPDATE operations SET status='pending',cancel_requested=0 WHERE id=?", (ident,))
                st.conn.execute("UPDATE operation_stages SET status='pending' WHERE operation_id=? AND status<>'succeeded'", (ident,))
        advance(cfg, ident)
    return show(cfg, ident)


def reconcile(cfg):
    """Fail closed after runner loss. Never release a possibly surviving process tree."""
    from lockstep.jobs import process_identity
    uncertain = []
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        for row in st.conn.execute("SELECT * FROM jobs WHERE status IN ('starting','running')").fetchall():
            job = dict(row)
            if not job.get('operation_id'):
                continue  # Older runners do not provide identity/heartbeat contracts.
            age = time.time() - (job.get('heartbeat_at') or job.get('started_at') or time.time())
            if age < 120 or (job.get('pid') and process_identity(job['pid']) == job.get('process_start')):
                continue
            st.conn.execute("UPDATE jobs SET status='unknown_execution' WHERE id=?", (job['id'],))
            uncertain.append(job['id'])
    advance(cfg)
    return {'unknown_execution': uncertain, 'automatic_retries': 0}
