"""Reconcile reservations only after evidence that their process tree stopped."""
import json
import subprocess
import time

from lockstep.artifacts import canonical
from lockstep.store import Store


STALE_RUNNER_GRACE_SECONDS = 120


def scope_stopped(unit):
    if not unit: return None
    try:
        result = subprocess.run(['systemctl','--user','show',unit,'--property=ActiveState','--property=LoadState'],capture_output=True,text=True,timeout=5)
    except (OSError,subprocess.TimeoutExpired): return None
    fields = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    # A transient unit that systemd has already collected can be reported as
    # not-found with a nonzero exit status.  The missing unit is still definite
    # evidence that its cgroup is gone; other command failures remain unknown.
    if fields.get('LoadState') == 'not-found':
        return True
    if result.returncode or 'ActiveState' not in fields:
        return None
    return fields['ActiveState'] in ('inactive','failed')


def wait_scope_stopped(unit, timeout_s=5.0, poll_s=0.05):
    """Wait briefly for systemd's asynchronous active-to-stopped transition.

    An unavailable bus is not evidence about the cgroup, so return uncertainty
    immediately.  A scope that stays active through the deadline is likewise
    left for explicit reconciliation rather than releasing its reservation.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        stopped = scope_stopped(unit)
        if stopped is not False:
            return stopped
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_s, remaining))


def _reconcile_rows(cfg, rows):
    from lockstep.jobs import process_identity
    released=[]; uncertain=[]
    for row in rows:
        status = row.get('status')
        if status in ('starting', 'running'):
            last_seen = row.get('heartbeat_at') or row.get('started_at') or row.get('created_at') or time.time()
            if time.time() - last_seen < STALE_RUNNER_GRACE_SECONDS:
                continue
        if row.get('pid') and process_identity(row['pid']) == row.get('process_start'):
            uncertain.append(row['id']); continue
        if scope_stopped(row.get('scope_unit')) is not True:
            uncertain.append(row['id']); continue
        with Store(cfg) as st:
            if 'memory_gib' in row:
                changed = st.conn.execute(
                    'DELETE FROM resource_leases WHERE id=? AND pid IS ? AND process_start IS ? AND scope_unit IS ?',
                    (row['id'], row.get('pid'), row.get('process_start'), row.get('scope_unit'))).rowcount
            else:
                try:
                    result = json.loads(row.get('result_json') or '{}')
                except (TypeError, json.JSONDecodeError):
                    result = {'prior_result_json': row.get('result_json')}
                if not isinstance(result, dict):
                    result = {'prior_result': result}
                result.update(execution_reconciled='scope_stopped', effects='require_review', termination_acknowledged=True)
                if status in ('starting', 'running'):
                    result['runner_reconciled_from'] = status
                changed = st.conn.execute(
                    "UPDATE jobs SET status='failed',finished_at=COALESCE(finished_at,?),exit_code=COALESCE(exit_code,1),result_json=? "
                    "WHERE id=? AND status=? AND pid IS ? AND process_start IS ? AND scope_unit IS ? AND heartbeat_at IS ?",
                    (time.time(), canonical(result).decode(), row['id'], status, row.get('pid'),
                     row.get('process_start'), row.get('scope_unit'), row.get('heartbeat_at'))).rowcount
        if changed:
            released.append(row['id'])
        else:
            uncertain.append(row['id'])
    return {'released_reservations':released,'unknown_execution':uncertain,'external_effects_retried':False}


def reconcile_stale_jobs(cfg):
    """Terminalize stale job rows only after their runner and exact scope stopped."""
    with Store(cfg) as st:
        jobs = [dict(r) for r in st.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('starting','running')")]
    return _reconcile_rows(cfg, jobs)


def reconcile(cfg):
    with Store(cfg) as st:
        leases = [dict(r) for r in st.conn.execute('SELECT * FROM resource_leases')]
        jobs = [dict(r) for r in st.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('starting','running','unknown_execution')")]
    return _reconcile_rows(cfg, leases + jobs)
