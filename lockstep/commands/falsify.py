from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from lockstep import EXIT_CHECK_FAILED, EXIT_LEAN, EXIT_OK
from lockstep.context import Ctx
from lockstep.errors import UsageError
from lockstep.nodes import find_node
from lockstep.output import Result
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def run(ctx: Ctx, name: str, dataset: str | None = None, async_: bool = False,
        timeout: int | None = None, targeted: bool = False) -> Result:
    cfg = ctx.cfg
    n = find_node(cfg, name)
    if not n.falsifier:
        raise UsageError(f"{n.name} has no @falsifier tag")
    script = cfg.falsifiers_dir / n.falsifier
    if not script.exists():
        raise UsageError(f"falsifier {script.relative_to(cfg.root)} is missing")
    if async_:
        from lockstep.jobs import submit
        argv = ["falsify", n.name] + ([f"--dataset={dataset}"] if dataset else [])
        if targeted:
            argv.append("--targeted")
        job = submit(cfg, kind="falsify", node=n.name, argv=argv, agent=ctx.agent, budget_min=timeout or cfg.falsify_timeout_min)
        return Result(EXIT_OK, {"job": job}, [f"submitted job {job['id']}"])
    ds = Path(dataset).resolve() if dataset else cfg.dataset_dir
    out_dir = cfg.falsify_results_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{n.name}.json"
    witness_file = cfg.state_home / "tmp" / f"witness-{n.short}.json"
    witness_file.parent.mkdir(parents=True, exist_ok=True)
    witness_file.unlink(missing_ok=True)
    cmd = [str(script), "--dataset", str(ds), "--out", str(witness_file), "--node", n.name, "--statement", n.type_pp]
    if script.suffix == ".py":
        cmd = ["python3"] + cmd
    # falsifiers run solvers and enumerations: cap them like builds (systemd user scope, no swap)
    memory_max = cfg.falsify_memory_max
    if memory_max and shutil.which("systemd-run") is not None:
        cmd = ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={memory_max}", "-p", "MemorySwapMax=0", "--"] + cmd
    log = cfg.build_log_dir / f"falsify-{time.strftime('%Y%m%d-%H%M%S')}-{n.short}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(log, "w") as lf:
        try:
            proc = subprocess.run(cmd, cwd=cfg.root, stdout=lf, stderr=subprocess.STDOUT, timeout=(timeout or cfg.falsify_timeout_min) * 60,
                                  env=dict(os.environ, LOCKSTEP_PROJECT_ROOT=str(cfg.root)))
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 124
    seconds = time.time() - started
    result = {0: "pass", 2: "counterexample"}.get(rc, "timeout" if rc == 124 else "error")
    witness = None
    if witness_file.exists():
        try:
            witness = json.loads(witness_file.read_text())
        except json.JSONDecodeError:
            witness = {"raw": witness_file.read_text()[:4000]}
    record = {
        "node": n.name, "falsifier": n.falsifier, "falsifier_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "dataset": str(ds.relative_to(cfg.root)) if ds.is_relative_to(cfg.root) else str(ds),
        "statement_type_hash": n.type_hash, "result": result, "exit_code": rc, "seconds": round(seconds, 1),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agent": ctx.agent, "witness": witness,
    }
    if result in ("pass", "counterexample"):
        out_file.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    with Store(cfg) as st:
        st.event("falsify", n.name, ctx.agent, {"result": result, "rc": rc})
        if result == "counterexample":
            st.record_attempt(n.name, ctx.agent, "refuted", note="falsifier counterexample")
    from lockstep.commands.extract import run_extract
    from lockstep.errors import Refused

    res = Result()
    try:
        if targeted:
            from lockstep.commands.node import targeted_extract
            targeted_extract(ctx, n.module)
            res.line("targeted extraction: full index not refreshed")
        else:
            run_extract(ctx)
    except Refused as exc:
        # the result is recorded; nodes.json/status.md pick it up at the next full extract
        res.line(f"nodes.json not refreshed ({exc}); run `lockstep extract` when memory allows")
    res.line(f"{n.name}: falsifier {n.falsifier} -> {result} (rc={rc}, {seconds:.0f}s); log {log}")
    if result in ("pass", "counterexample"):
        res.line(f"recorded {out_file.relative_to(cfg.root)}")
    if witness and result == "counterexample":
        res.line("witness: " + json.dumps(witness)[:500])
    res.payload = {"record": record, "log": str(log), "targeted": targeted}
    res.exit_code = EXIT_OK if result == "pass" else (EXIT_CHECK_FAILED if result == "counterexample" else EXIT_LEAN)
    return res


command(CommandSpec(name="falsify", cli=["falsify"], long_running=True,
                    help="Run a node's falsifier (exit 0 pass, 2 counterexample with witness) and record the result under harness/falsify/.", run=run,
                    args=[Arg("name", "node name", positional=True, required=True), Arg("dataset", "dataset directory (default from config)"),
                          Arg("async_", "submit as a background job", "bool"), Arg("timeout", "minutes", "int"),
                          Arg("targeted", "extract only this node's import closure; leave the full index unchanged", "bool")]))
