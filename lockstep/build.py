"""Lake builds with a memory cgroup guard and a wall-clock budget."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from lockstep.config import Config
from lockstep.modules import Module, module_to_path, scan_modules, topological, transitive_closure


@dataclass
class BuildResult:
    rc: int
    seconds: float
    built: int
    log_path: str
    command: list[str]
    budget_exceeded: bool = False
    oom_kills: int = 0
    peak_mib: int = 0
    refused: str | None = None
    tail: list[str] = field(default_factory=list)
    termination_acknowledged: bool = True

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.budget_exceeded and self.refused is None and self.termination_acknowledged

    def summary(self) -> str:
        if self.refused:
            return f"refused: {self.refused}"
        status = "ok" if self.ok else ("budget exceeded" if self.budget_exceeded else (f"rc={self.rc} (scope stopped: memory cap hit, see `journalctl --user`)" if self.rc == -15 and not self.peak_mib else f"rc={self.rc}"))
        extra = f", peak {self.peak_mib} MiB" if self.peak_mib else ""
        extra += f", oom_kills={self.oom_kills}" if self.oom_kills else ""
        return f"{status}: {self.built} module(s) built in {self.seconds:.0f}s{extra}; log {self.log_path}"


def lean_compilers_running() -> int:
    """Count `lean` compiler processes (language-server workers excluded)."""
    try:
        out = subprocess.run(["pgrep", "-x", "lean"], capture_output=True, text=True).stdout
    except OSError:
        return 0
    count = 0
    for pid in out.split():
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"--server" in cmd or b"--worker" in cmd:
            continue
        count += 1
    return count


_BUS_OK: bool | None = None


def bus_available(timeout_s: float = 10.0) -> bool:
    """Can this process create a transient user scope?  Agent sandboxes often hide the
    systemd user bus: `systemd-run` then fails with "Failed to connect to bus".  Cached per process."""
    global _BUS_OK
    if _BUS_OK is not None:
        return _BUS_OK
    if shutil.which("systemd-run") is None:
        _BUS_OK = False
        return False
    try:
        proc = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=64M", "--", "true"],
                              capture_output=True, text=True, timeout=timeout_s)
        _BUS_OK = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        _BUS_OK = False
    return _BUS_OK


def available_gib() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1048576
    except OSError:
        pass
    return float("inf")


def _mem_gib(spec: str) -> float:
    s = spec.strip().upper()
    if s.endswith("G"):
        return float(s[:-1])
    if s.endswith("M"):
        return float(s[:-1]) / 1024
    return float(s) / 2**30


def run_guarded(cfg: Config, argv: list[str], **kwargs) -> BuildResult:
    """Account synchronous calls too; nested stages inherit their enclosing cgroup."""
    from lockstep.store import Store
    from lockstep.jobs import live_memory_gib
    import copy
    job_id = kwargs.get('job_id') or os.environ.get('LOCKSTEP_JOB_ID')
    memory = kwargs.get('memory_max') or cfg.build_memory_max
    cap = _mem_gib(memory)
    reservation = None
    is_lake_build = argv[:2] == [cfg.lake, 'build']
    storage_targets = []
    if is_lake_build and cfg.build_artifact_cache_enabled:
        from lockstep.artifact_cache import default_cache_root, reject_symlink_ancestors
        from lockstep.storage import StorageTarget

        cache_root = cfg.build_artifact_cache_root or default_cache_root(cfg)
        reject_symlink_ancestors(cache_root)
        cache_root = Path(os.path.abspath(os.fspath(cache_root)))
        storage_targets.append(StorageTarget('artifact-cache', cache_root))
        env_extra = dict(kwargs.get('env_extra') or {})
        env_extra.update(LAKE_ARTIFACT_CACHE='true', LAKE_CACHE_DIR=str(cache_root))
        kwargs['env_extra'] = env_extra
    nested = os.environ.get('LOCKSTEP_IN_GUARDED_STAGE') == '1'
    if nested:
        outer_cap = os.environ.get('LOCKSTEP_STAGE_MEMORY', '0')
        if cap > _mem_gib(outer_cap):
            return BuildResult(125, 0, 0, str(kwargs['log_path']), argv, refused=f'nested stage requests {memory} above enclosing cap {outer_cap}')
        cfg = copy.deepcopy(cfg)
        cfg.raw.setdefault('build', {})['guard'] = False
        kwargs['force'] = True
    try:
        from lockstep.storage import reserve_storage_in_transaction

        with Store(cfg) as st:
            st.conn.execute('BEGIN IMMEDIATE')
            if job_id:
                job = st.job(job_id)
                if not job or job['status'] != 'running':
                    return BuildResult(125, 0, 0, str(kwargs['log_path']), argv, refused='enclosing job is not running')
                reserved = job.get('memory_max') or cfg.build_memory_max
                if cap > _mem_gib(reserved):
                    return BuildResult(125, 0, 0, str(kwargs['log_path']), argv, refused=f'requested {memory} exceeds job reservation {reserved}')
                kwargs['job_id'] = job_id
                # The outer scope of `lockstep job run` guards arbitrary computation,
                # not a worktree writer.  If that command starts a nested
                # harness Lake build, the inner call acquires this target.
                if job.get('kind') != 'run' or is_lake_build:
                    reservation = reserve_storage_in_transaction(
                        st, cfg, roots=[cfg.root], targets=storage_targets,
                        reason='guarded command', owner_job_id=job_id
                    )
            else:
                jobs = [dict(r) for r in st.conn.execute("SELECT * FROM jobs WHERE status IN ('starting','running','unknown_execution')")]
                leased = st.conn.execute('SELECT COALESCE(SUM(memory_gib),0) FROM resource_leases').fetchone()[0]
                if cap + live_memory_gib(cfg, jobs) + leased > _mem_gib(cfg.jobs_memory_budget):
                    return BuildResult(125, 0, 0, str(kwargs['log_path']), argv, refused=f'{memory} cannot fit shared budget {cfg.jobs_memory_budget}')
                reservation = reserve_storage_in_transaction(
                    st, cfg, roots=[cfg.root], targets=storage_targets,
                    reason='guarded command', memory_gib=cap
                )
    except Exception as exc:
        from lockstep.errors import CheckFailed

        if isinstance(exc, CheckFailed):
            return BuildResult(125, 0, 0, str(kwargs['log_path']), argv, refused=str(exc))
        raise
    result = None
    try:
        result = _run_guarded(cfg, argv, lease_ids=reservation.lease_ids if reservation else (), **kwargs)
        return result
    finally:
        if reservation and result and result.termination_acknowledged:
            from lockstep.storage import release_storage_reservation

            release_storage_reservation(cfg, reservation)


def _run_guarded(
    cfg: Config,
    argv: list[str],
    *,
    log_path: Path,
    budget_min: float | None,
    memory_max: str | None = None,
    cpu_quota_cores: int | None = None,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
    force: bool = False,
    count_built: bool = True,
    job_id: str | None = None,
    lease_ids: tuple[str, ...] = (),
) -> BuildResult:
    """Run ``argv`` (a lake/lean command) inside a transient user cgroup with a memory cap."""
    memory_max = memory_max or cfg.build_memory_max
    cpu_quota_cores = cfg.build_cpu_quota if cpu_quota_cores is None else cpu_quota_cores
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if cfg.build_threads:
        env["LEAN_NUM_THREADS"] = str(cfg.build_threads)
    if env_extra:
        env.update(env_extra)
    if not force:
        running = lean_compilers_running()
        if running >= max(1, cfg.max_parallel_builds):
            return BuildResult(125, 0, 0, str(log_path), argv, refused=f"{running} lean compiler(s) already running (use --force to override)")
        need = _mem_gib(memory_max) + 6
        if cfg.build_guard and available_gib() < need:
            return BuildResult(125, 0, 0, str(log_path), argv, refused=f"only {available_gib():.0f} GiB available; need {need:.0f} GiB for a {memory_max} guard")
    if cfg.build_guard and shutil.which('systemd-run') is None:
        return BuildResult(125,0,0,str(log_path),argv,refused='systemd-run is required for guarded execution')
    use_guard = cfg.build_guard
    unit = f"lockstep-{uuid.uuid4().hex[:10]}"
    if use_guard:
        from lockstep.store import Store
        with Store(cfg) as st:
            if job_id: st.conn.execute('UPDATE jobs SET scope_unit=? WHERE id=?',(unit+'.scope',job_id))
            if lease_ids:
                placeholders = ','.join('?' for _ in lease_ids)
                st.conn.execute(
                    f'UPDATE resource_leases SET scope_unit=? WHERE id IN ({placeholders})',
                    (unit+'.scope', *lease_ids),
                )
    stats = log_path.with_suffix(".stats")
    if use_guard:
        inner = (
            "\"$@\"; rc=$?; "
            "cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); "
            "{ cat \"$cg/memory.peak\" 2>/dev/null || echo 0; "
            "awk '/^oom_kill /{print $2}' \"$cg/memory.events\" 2>/dev/null || echo 0; } > \"$LOCKSTEP_STATS\"; "
            "exit $rc"
        )
        props = ["-p", f"MemoryMax={memory_max}", "-p", "MemorySwapMax=0"]
        if cpu_quota_cores:
            props += ["-p", f"CPUQuota={cpu_quota_cores * 100}%"]
        full = ["systemd-run", "--user", "--scope", "-q", f"--unit={unit}", *props, "--", "bash", "-c", inner, "_", *argv]
        env["LOCKSTEP_STATS"] = str(stats)
    else:
        full = list(argv)
    start = time.time()
    budget_s = budget_min * 60 if budget_min else None
    budget_exceeded = False
    with open(log_path, "w") as log:
        log.write("$ " + " ".join(argv) + "\n")
        log.flush()
        proc = subprocess.Popen(full, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=cwd or cfg.root, start_new_session=True)
        cancelled = False
        last_heartbeat = 0.0
        try:
            if job_id:
                from lockstep.store import Store
                while proc.poll() is None:
                    with Store(cfg) as st:
                        row = st.job(job_id)
                        cancelled = bool(row and row.get('cancel_requested'))
                        if time.time()-last_heartbeat >= 5:
                            last_heartbeat=time.time()
                            st.job_update(job_id, heartbeat_at=last_heartbeat)
                            if row and row.get('operation_id') and row.get('node'):
                                operation=st.conn.execute('SELECT created_at,agent FROM operations WHERE id=?',(row['operation_id'],)).fetchone()
                                if operation and last_heartbeat-operation['created_at'] < 86400:
                                    st.conn.execute('UPDATE claims SET expires_at=MAX(expires_at,?) WHERE node=? AND agent=?',
                                                    (min(last_heartbeat+3600,operation['created_at']+86400),row['node'],operation['agent']))
                    if cancelled or (budget_s and time.time() - start >= budget_s):
                        raise subprocess.TimeoutExpired(full, budget_s or 0)
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
                rc = proc.returncode
            else:
                rc = proc.wait(timeout=budget_s)
        except subprocess.TimeoutExpired:
            budget_exceeded = not cancelled
            if use_guard:
                subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True, timeout=30)
            try:
                os.killpg(proc.pid, 15)
            except OSError:
                pass
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                rc = proc.wait()
    seconds = time.time() - start
    peak = ooms = 0
    if stats.exists():
        try:
            lines = stats.read_text().split()
            peak = int(lines[0]) // 1048576 if lines else 0
            ooms = int(lines[1]) if len(lines) > 1 else 0
        except (ValueError, OSError):
            pass
        stats.unlink(missing_ok=True)
    if count_built and (rc == -15 or budget_exceeded):
        # the stopped scope's compilers take a few seconds to exit; do not let the next
        # invocation be refused for "a compiler is already running"
        for _ in range(60):
            if lean_compilers_running() == 0:
                break
            time.sleep(2)
    text = log_path.read_text(errors="replace")
    built = sum(1 for line in text.splitlines() if "Built " in line) if count_built else 0
    tail = text.splitlines()[-15:]
    if use_guard and rc != 0 and any(m in text for m in ("Failed to connect to bus", "Failed to start transient scope")):
        return BuildResult(rc, seconds, 0, str(log_path), argv, refused=(
            "cannot create a memory-guarded scope from this process (the systemd user bus is unreachable: a sandbox?); "
            "run this from an unsandboxed shell, or one that can reach /run/user/<uid>/bus"), tail=tail)
    from lockstep.resources import wait_scope_stopped
    acknowledged = not use_guard or wait_scope_stopped(unit+'.scope') is True
    return BuildResult(rc, seconds, built, str(log_path), argv, budget_exceeded, ooms, peak, tail=tail, termination_acknowledged=acknowledged)


def largest_build_memory(cfg: Config) -> str:
    """The largest cap any stage of a harness build can request: the default, a
    [[build.memory_overrides]] match, the single-module retry and the final parallel step.
    A background build or check job reserves this much, since none of its stages may exceed
    the job's reservation."""
    retry = str(cfg.get("build", "memory_max_retry", default="30G"))
    caps = [cfg.build_memory_max, retry, str(cfg.get("build", "memory_max_final", default=retry))]
    caps += [str(o["memory_max"]) for o in cfg.get("build", "memory_overrides", default=[]) or [] if o.get("memory_max")]
    return max(caps, key=_mem_gib)


def memory_for(cfg: Config, modules: list[str]) -> str:
    """The memory cap for a lake invocation: the largest [[build.memory_overrides]] match, else the default."""
    import fnmatch
    best = _mem_gib(cfg.build_memory_max)
    spec = cfg.build_memory_max
    for o in cfg.get("build", "memory_overrides", default=[]) or []:
        pat, mem = str(o.get("pattern", "")), str(o.get("memory_max", ""))
        if mem and any(fnmatch.fnmatchcase(m, pat) for m in modules) and _mem_gib(mem) > best:
            best, spec = _mem_gib(mem), mem
    return spec


def build_module_group(cfg: Config, group: list[str], *, log: Path, budget_min: float | None, force: bool,
                       extra_args: list[str] | None = None) -> BuildResult:
    """One guarded lake invocation; a single module that hits the cap is retried once with the escalation cap."""
    mem = memory_for(cfg, group)
    res = run_guarded(cfg, [cfg.lake, "build", *group, *(extra_args or [])], log_path=log, budget_min=budget_min, memory_max=mem, force=force)
    if res.rc == -15 and not res.budget_exceeded and len(group) == 1:
        bigger = str(cfg.get("build", "memory_max_retry", default="30G"))
        if _mem_gib(bigger) > _mem_gib(mem):
            log2 = log.with_name(log.stem + "-retry" + log.suffix)
            res2 = run_guarded(cfg, [cfg.lake, "build", *group, *(extra_args or [])], log_path=log2, budget_min=budget_min, memory_max=bigger, force=force)
            res2.tail = [f"(retried with MemoryMax={bigger} after the {mem} cap was hit)"] + res2.tail
            return res2
    return res


def missing_olean_modules(cfg: Config, mods: dict[str, Module], roots: list[str]) -> list[str]:
    """Modules in the closure of ``roots`` whose olean does not exist, dependencies first."""
    lib = cfg.root / ".lake" / "build" / "lib" / "lean"
    closure = transitive_closure(mods, roots)
    missing = {m for m in closure if not (lib / module_to_path(m)).with_suffix(".olean").exists()}
    return topological(mods, missing)


def lake_out_of_date(cfg: Config, targets: list[str], timeout_s: int = 900) -> list[str] | None:
    """Modules Lake reports as needing a rebuild for ``targets`` (``lake build --no-build``); None if unknown."""
    try:
        proc = subprocess.run([cfg.lake, "build", "--no-build", *targets], capture_output=True, text=True, cwd=cfg.root, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out: list[str] = []
    for line in proc.stdout.splitlines() + proc.stderr.splitlines():
        if line.startswith("- ") and " " not in line[2:].strip():
            out.append(line[2:].strip())
    if proc.returncode == 0 and not out:
        return []
    return out or None


def lake_targets_for(cfg: Config, kind: str, node_module: str | None = None) -> list[str]:
    if kind == "node":
        if not node_module:
            raise ValueError("node build needs a module")
        return [node_module]
    return cfg.build_targets(kind)


def build(cfg: Config, kind: str, *, budget_min: float | None = None, node_module: str | None = None,
          force: bool = False, serial: bool | None = None, extra_args: list[str] | None = None) -> list[BuildResult]:
    """Build a configured target kind. Returns one result per lake invocation."""
    targets = lake_targets_for(cfg, kind, node_module)
    log_dir = cfg.build_log_dir
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results: list[BuildResult] = []
    serial = cfg.build_serial_missing if serial is None else serial
    if serial and kind != "certs":
        mods = scan_modules(cfg)
        roots = [t for t in targets if t in mods]
        # library targets are named after their root module in our layouts; map known lib names
        for t in targets:
            if t not in mods:
                root_mod = cfg.get("build", "target_roots", t)
                if root_mod:
                    roots.append(str(root_mod))
        # Lake reports only the first stale module of each dependency chain, so iterate:
        # build what it lists, one module per (guarded) invocation, until nothing is listed.
        seen: set[str] = set()
        deadline = time.time() + (budget_min * 60 if budget_min else 10**9)
        for _round in range(int(cfg.get("build", "serial_rounds", default=25))):
            stale = lake_out_of_date(cfg, targets)
            if stale is None:
                stale = missing_olean_modules(cfg, mods, roots)
            else:
                stale = topological(mods, set(stale) & set(mods)) or stale
            stale = [m for m in stale if m not in seen]
            if not stale:
                break
            # Chain heads are independent of each other: build small ones a few per
            # invocation (one Lake start-up each is the dominant cost for hundreds of
            # modules); heavy ones (checkpoint segments etc.) strictly one at a time.
            # A batch that trips the memory cap is retried module by module.
            heavy_pats = [str(x) for x in cfg.get("build", "heavy_patterns", default=["*CheckpointSegment*", "*CheckpointContext", "*CNFRaw*", "*CNFMid*"])]
            batch_size = int(cfg.get("build", "batch_small", default=6))
            import fnmatch
            def heavy(m: str) -> bool:
                return any(fnmatch.fnmatchcase(m, p) for p in heavy_pats)
            groups: list[list[str]] = []
            small: list[str] = []
            for m in stale:
                seen.add(m)
                if heavy(m):
                    groups.append([m])
                else:
                    small.append(m)
                    if len(small) >= batch_size:
                        groups.append(small); small = []
            if small:
                groups.append(small)
            for group in groups:
                remaining = (deadline - time.time()) / 60
                if remaining <= 0:
                    results.append(BuildResult(124, 0, 0, "", [cfg.lake, "build", *group], budget_exceeded=True))
                    return results
                log = log_dir / f"build-{stamp}-{group[0]}{'+%d' % (len(group) - 1) if len(group) > 1 else ''}.log"
                res = build_module_group(cfg, group, log=log, budget_min=remaining, force=force, extra_args=extra_args)
                results.append(res)
                if not res.ok and len(group) > 1 and not res.budget_exceeded:
                    # retry one at a time (memory cap or a single failing module)
                    for m in group:
                        remaining = (deadline - time.time()) / 60
                        if remaining <= 0:
                            return results
                        log = log_dir / f"build-{stamp}-{m}.log"
                        res = build_module_group(cfg, [m], log=log, budget_min=remaining, force=force, extra_args=extra_args)
                        results.append(res)
                        if not res.ok:
                            return results
                elif not res.ok:
                    return results
        budget_min = max(1.0, (deadline - time.time()) / 60) if budget_min else None
        # Never hand a still-stale tree to the fully parallel build: a memory-cap kill
        # there destroys the oleans of every module in flight.
        left = lake_out_of_date(cfg, targets)
        if left:
            results.append(BuildResult(124, 0, 0, "", [cfg.lake, "build", *targets], budget_exceeded=True,
                                       tail=[f"{len(left)} chain head(s) still out of date after the serial pass (budget exhausted?): " + ", ".join(left[:8])]))
            return results
    log = log_dir / f"build-{stamp}-{kind}.log"
    # the final (parallel) step maps the whole library into the root module's compiler; give it the larger cap
    final_mem = str(cfg.get("build", "memory_max_final", default=cfg.get("build", "memory_max_retry", default="30G")))
    results.append(run_guarded(cfg, [cfg.lake, "build", *targets, *(extra_args or [])], log_path=log, budget_min=budget_min, memory_max=final_mem, force=force))
    return results
