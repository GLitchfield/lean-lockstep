"""Background jobs: submitted rows in SQLite, executed by a detached `lockstep job exec` or by `lockstep worker`.

Two kinds of job: harness commands (build, check, falsify: ``argv`` is an `lockstep` argument
list) and ``run`` jobs (`lockstep job run`: ``argv`` is an arbitrary command, executed inside
the memory-guarded scope with its purpose, cap, outputs and measured peak recorded)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from lockstep.build import _mem_gib, available_gib, bus_available, largest_build_memory, run_guarded
from lockstep.config import Config
from lockstep.errors import CheckFailed
from lockstep.store import Store


def _es_argv() -> list[str]:
    return [sys.executable, "-m", "lockstep"]


def submit(cfg: Config, *, kind: str, node: str | None, argv: list[str], agent: str, budget_min: int | None = None,
           queue: bool = False) -> dict:
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    log_dir = cfg.build_log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"job-{job_id}.log"
    job = {
        "id": job_id, "kind": kind, "node": node, "argv": json.dumps(argv), "cwd": str(cfg.root),
        "status": "queued", "exit_code": None, "created_at": time.time(), "started_at": None, "finished_at": None,
        "log_path": str(log_path), "budget_min": budget_min, "agent": agent, "pid": None, "result_json": None,
        "memory_max": cfg.falsify_memory_max if kind == 'falsify' else largest_build_memory(cfg),
        "lock_key": 'worktree:' + str(cfg.root.resolve()),
    }
    with Store(cfg) as st:
        st.job_insert(job)
        st.event("job-submitted", node, agent, {"id": job_id, "kind": kind})
    if not queue:
        spawn_runner(cfg, job_id)
    return job


def job_memory_gib(cfg: Config, job: dict) -> float:
    """The memory cap a job runs under (its own for `run` jobs, the configured one otherwise)."""
    if job.get("memory_max"):
        return _mem_gib(str(job["memory_max"]))
    kind = job.get("kind")
    if kind == "falsify":
        return _mem_gib(cfg.falsify_memory_max or "0")
    if kind in ("build", "check"):
        return _mem_gib(largest_build_memory(cfg))
    return 0.0


def live_memory_gib(cfg: Config, jobs: list[dict]) -> float:
    return sum(job_memory_gib(cfg, j) for j in jobs)


def budget_problem(cfg: Config, cap: float, memory_max: str, running: list[dict] | None = None) -> str | None:
    """Why a job with cap ``cap`` GiB cannot start now (None if it can): the summed caps of running
    jobs plus this one must stay under [jobs] memory_budget, and cap + margin GiB must be available."""
    if running is None:
        with Store(cfg) as st:
            running = [j for j in st.live_jobs() if j["status"] in ("running", "starting")]
    used = live_memory_gib(cfg, running)
    budget = _mem_gib(cfg.jobs_memory_budget)
    if used + cap > budget:
        names = ", ".join(f"{j['id']} ({j.get('memory_max') or j['kind']})" for j in running)
        return f"memory budget: {used:.0f} GiB of caps already running ({names}) + {cap:.0f} GiB > budget {budget:.0f} GiB"
    avail = available_gib()
    if avail < cap + cfg.jobs_run_margin_gib:
        return f"only {avail:.0f} GiB available; a {memory_max} job needs {cap + cfg.jobs_run_margin_gib:.0f} GiB"
    return None


def process_identity(pid: int | None) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def reserve_job(cfg, job_id, *, launching=False):
    """Atomic admission across workers and direct runners; no automatic stale retry."""
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        row = st.conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            return False
        job = dict(row)
        allowed = ('queued',) if launching else ('queued', 'starting')
        if job['status'] not in allowed or job.get('cancel_requested'):
            return False
        running = [dict(r) for r in st.conn.execute("SELECT * FROM jobs WHERE status IN ('starting','running','unknown_execution') AND id<>?", (job_id,))]
        leases = [dict(r) for r in st.conn.execute('SELECT * FROM resource_leases')]
        if job.get('lock_key') and any(r.get('lock_key') == job['lock_key'] for r in running):
            return False
        if job.get('lock_key') and any(r.get('lock_key') == job['lock_key'] for r in leases):
            return False
        from lockstep.storage import _worktree_owners
        build_owners = _worktree_owners(running, leases)
        if (job.get('kind') in ('build','check') or str(job.get('lock_key') or '').startswith('worktree:')) and len(build_owners) >= cfg.max_parallel_builds:
            return False
        cap = job_memory_gib(cfg, job)
        if live_memory_gib(cfg, running) + cap + sum(r['memory_gib'] for r in leases) > _mem_gib(cfg.jobs_memory_budget):
            return False
        if cfg.build_guard and budget_problem(cfg, cap, job.get('memory_max') or '', running):
            return False
        st.conn.execute('UPDATE jobs SET status=?,started_at=COALESCE(started_at,?),pid=?,process_start=?,heartbeat_at=? WHERE id=?',
                        ('starting' if launching else 'running', time.time(), None if launching else os.getpid(),
                         None if launching else process_identity(os.getpid()), time.time(), job_id))
        return True


def request_cancel(cfg, job_id):
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        job = st.job(job_id)
        if job is None:
            raise CheckFailed(f'unknown job {job_id}')
        if job['status'] in ('done', 'failed', 'killed'):
            return job
        if job['status'] == 'queued':
            st.conn.execute("UPDATE jobs SET status='killed',cancel_requested=1,finished_at=?,exit_code=130 WHERE id=?", (time.time(), job['id']))
        else:
            st.conn.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job['id'],))
        st.conn.execute('INSERT INTO events(at,kind,node,agent,detail) VALUES (?,?,?,?,?)',
                        (time.time(), 'job-cancel-requested', job.get('node'), os.environ.get('LOCKSTEP_AGENT'), json.dumps({'id':job['id']})))
        return dict(st.conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())


def check_outputs(cfg: Config, outputs: list[str]) -> list[str]:
    """Normalize `--out` paths and refuse any outside the configured output roots."""
    roots = cfg.jobs_output_roots
    out = []
    for o in outputs:
        p = (cfg.root / o).resolve()
        try:
            rel = p.relative_to(cfg.root)
        except ValueError:
            raise CheckFailed(f"output {o} is outside the project; results must land under {', '.join(roots)}/")
        if not any(rel == Path(r) or Path(r) in rel.parents for r in roots):
            raise CheckFailed(f"output {o} is not under {', '.join(roots)}/ (results must be findable by other agents, not in a scratch directory)")
        out.append(str(rel))
    return out


def submit_run(cfg: Config, *, command: list[str], why: str, outputs: list[str], agent: str, node: str | None = None,
               memory_max: str | None = None, budget_min: int | None = None, cpu_cores: int | None = None,
               queue: bool = False) -> dict:
    """Submit an arbitrary command as a guarded, recorded job."""
    if not command:
        raise CheckFailed("no command given (put it after `--`)")
    if not why or not why.strip():
        raise CheckFailed("--why is required: say what the job is for")
    memory_max = memory_max or cfg.jobs_default_memory_max
    outs = check_outputs(cfg, outputs)
    if not outs:
        raise CheckFailed("--out is required: where the results will be written (under " + ", ".join(cfg.jobs_output_roots) + "/)")
    cap = _mem_gib(memory_max)
    for o in outs:
        # the job writes here; create the directory so a script that only opens the file works
        (cfg.root / o).parent.mkdir(parents=True, exist_ok=True)
    budget = _mem_gib(cfg.jobs_memory_budget)
    if cap > budget:
        raise CheckFailed(f"--mem {memory_max} exceeds the memory budget {cfg.jobs_memory_budget}")
    if not queue:
        # a job started now must fit; a queued one is started by `lockstep worker` when it fits
        problem = budget_problem(cfg, cap, memory_max)
        if problem:
            raise CheckFailed(problem + "; wait, cancel a job, lower --mem, or --queue it for `lockstep worker`")
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    log_dir = cfg.build_log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id, "kind": "run", "node": node, "argv": json.dumps(command), "cwd": str(cfg.root),
        "status": "queued", "exit_code": None, "created_at": time.time(), "started_at": None, "finished_at": None,
        "log_path": str(log_dir / f"job-{job_id}.log"), "budget_min": budget_min, "agent": agent, "pid": None, "result_json": None,
        "why": why.strip(), "memory_max": memory_max, "outputs": json.dumps(outs), "peak_mib": None, "seconds": None,
    }
    if cpu_cores:
        job["result_json"] = json.dumps({"cpu_cores": cpu_cores})
    # a process that cannot create a guarded scope (a sandbox without the systemd user bus)
    # leaves the job queued for an `lockstep worker` running outside the sandbox
    job["queued_for_worker"] = False
    if not queue and cfg.build_guard and not bus_available():
        queue = True
        job["queued_for_worker"] = True
    row = {k: v for k, v in job.items() if k != "queued_for_worker"}
    with Store(cfg) as st:
        st.job_insert(row)
        st.event("job-submitted", node, agent, {"id": job_id, "kind": "run", "why": job["why"], "memory_max": memory_max,
                                                "outputs": outs, "command": command, "queued": queue})
    if not queue:
        spawn_runner(cfg, job_id)
    return job


def spawn_runner(cfg: Config, job_id: str) -> None:
    env = dict(os.environ)
    env["LOCKSTEP_PROJECT_ROOT"] = str(cfg.root)
    subprocess.Popen(
        _es_argv() + ["job", "exec", job_id],
        cwd=cfg.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def run_job(cfg: Config, job_id: str) -> dict:
    with Store(cfg) as st:
        submitted = st.job(job_id)
    if submitted is None:
        raise CheckFailed(f'unknown job {job_id}')
    job_id = submitted['id']
    if Path(submitted['cwd']).resolve() != cfg.root.resolve():
        from lockstep.config import load_config
        cfg = load_config(Path(submitted['cwd']).resolve())
    if not reserve_job(cfg, job_id):
        with Store(cfg) as st:
            return st.job(job_id) or {}
    with Store(cfg) as st:
        job = st.job(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
    argv = json.loads(job["argv"])
    env = dict(os.environ)
    env["LOCKSTEP_PROJECT_ROOT"] = str(cfg.root)
    if job.get("agent"):
        env["LOCKSTEP_AGENT"] = job["agent"]
    env['LOCKSTEP_JOB_ID'] = job['id']
    env['LOCKSTEP_STAGE_MEMORY'] = job.get('memory_max') or cfg.build_memory_max
    if job['kind'] == 'operation':
        from lockstep.operations import execute_stage
        return execute_stage(cfg, job)
    if job["kind"] == "run":
        return _run_command_job(cfg, job, argv)
    with open(job["log_path"], "a") as log:
        proc = subprocess.run(_es_argv() + ["--json", *argv], cwd=job["cwd"], env=env, stdout=log, stderr=subprocess.STDOUT)
    rc = proc.returncode
    result_json = None
    try:
        text = Path(job["log_path"]).read_text()
        start = text.rfind("\n{")
        if start >= 0 or text.lstrip().startswith('{'):
            result_json = text[start + 1 :] if start >= 0 else text.strip()
            json.loads(result_json)
    except (OSError, json.JSONDecodeError):
        result_json = None
    with Store(cfg) as st:
        st.job_update(job["id"], status="done" if rc == 0 else "failed", exit_code=rc, finished_at=time.time(), result_json=result_json)
        st.event("job-finished", job.get("node"), job.get("agent"), {"id": job["id"], "rc": rc})
        return st.job(job["id"]) or job


def _run_command_job(cfg: Config, job: dict, argv: list[str]) -> dict:
    """Execute a `run` job inside the guarded scope and record what it used."""
    cpu = None
    try:
        cpu = (json.loads(job.get("result_json") or "{}") or {}).get("cpu_cores")
    except json.JSONDecodeError:
        cpu = None
    memory_max = job.get("memory_max") or cfg.jobs_default_memory_max
    # A long-lived shared worker may have been started from another worktree or
    # with a one-shot extractor override.  The guarded child belongs to this
    # recorded job, so give nested `lockstep` commands its root and reservation rather
    # than inheriting the worker's process environment.
    env = {
        "LOCKSTEP_AGENT": job.get("agent") or "",
        "LOCKSTEP_JOB_ID": job["id"],
        "LOCKSTEP_PROJECT_ROOT": str(cfg.root),
        "LOCKSTEP_STAGE_MEMORY": memory_max,
        "LOCKSTEP_IN_GUARDED_STAGE": "1",
        "LOCKSTEP_EXTRACT_MEMORY_MAX": str(cfg.get("extract", "memory_max", default="16G")),
    }
    # the budget and availability were checked at submission; `force` skips the
    # lean-compiler check, which is about lake builds, not about arbitrary commands
    res = run_guarded(cfg, argv, log_path=Path(job["log_path"]), budget_min=job.get("budget_min"),
                      memory_max=memory_max, cpu_quota_cores=cpu or 0, env_extra=env,
                      cwd=Path(job["cwd"]), force=True, count_built=False,
                      job_id=job['id'])
    outputs = json.loads(job.get("outputs") or "[]")
    present = [o for o in outputs if (cfg.root / o).exists()]
    with Store(cfg) as st:
        cancelled = st.job(job['id']).get('cancel_requested')
    status = 'killed' if cancelled else ("done" if res.rc == 0 and not res.budget_exceeded else ("killed" if res.budget_exceeded else "failed"))
    if not res.termination_acknowledged: status = 'unknown_execution'
    result = {"rc": res.rc, "seconds": round(res.seconds, 1), "peak_mib": res.peak_mib, "oom_kills": res.oom_kills,
              "budget_exceeded": res.budget_exceeded, "outputs_present": present, "outputs_missing": [o for o in outputs if o not in present],
              "refused": res.refused, "tail": res.tail[-5:], 'termination_acknowledged':res.termination_acknowledged}
    with Store(cfg) as st:
        st.job_update(job["id"], status=status, exit_code=res.rc, finished_at=time.time(), peak_mib=res.peak_mib,
                      seconds=round(res.seconds, 1), result_json=json.dumps(result))
        st.event("job-finished", job.get("node"), job.get("agent"), {"id": job["id"], "kind": "run", "rc": res.rc, "status": status,
                                                                    "seconds": round(res.seconds, 1), "peak_mib": res.peak_mib,
                                                                    "memory_max": job.get("memory_max"), "why": job.get("why"),
                                                                    "outputs_present": present})
        return st.job(job["id"]) or job


def worker_start_fitting(cfg: Config, spawn=None) -> list[str]:
    """Start every queued job that fits the memory budget right now (oldest first, a small job may
    pass a large one).  Each starts in a detached runner, so several run at once.  Returns the ids."""
    spawn = spawn or spawn_runner
    started: list[str] = []
    with Store(cfg) as st:
        live = st.live_jobs()
    running = [j for j in live if j["status"] in ("running", "starting")]
    queued = sorted((j for j in live if j["status"] == "queued"), key=lambda j: j["created_at"])
    for job in queued:
        cap = job_memory_gib(cfg, job)
        if budget_problem(cfg, cap, job.get("memory_max") or "", running):
            # Reserve the next slot for an aged large job rather than allowing
            # an endless stream of younger small jobs to starve it.
            if cap <= _mem_gib(cfg.jobs_memory_budget) and time.time() - job['created_at'] > float(cfg.get('jobs','aging_seconds',default=300)):
                break
            continue
        if not reserve_job(cfg, job['id'], launching=True):
            continue
        try:
            spawn(cfg, job["id"])
        except Exception:
            with Store(cfg) as st:
                st.job_update(job['id'], status='queued', started_at=None)
            raise
        running.append(job)
        started.append(job["id"])
    return started


def worker_loop(cfg: Config, poll_s: float = 5.0, once: bool = False) -> None:
    if cfg.build_guard and not bus_available():
        raise CheckFailed("this process cannot reach the systemd user bus, so it cannot run guarded jobs; start `lockstep worker` from an unsandboxed shell")
    with Store(cfg) as st:
        st.event("worker-started", None, os.environ.get("LOCKSTEP_AGENT"), {"pid": os.getpid()})
    while True:
        try:
            from lockstep.operations import advance
            advance(cfg)
            worker_start_fitting(cfg)
        except Exception as exc:  # a transient database error must not stop the worker
            print(f"worker: {exc}", file=sys.stderr, flush=True)
        if once:
            return
        time.sleep(poll_s)
