"""The audit trail (design §8.4): recorded jobs with a memory budget, intent on claims, outcomes on
releases, the journal, the staleness warning and the hook."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lockstep.cli import main
from lockstep.errors import CheckFailed
from lockstep.nodes import Node, compute
from lockstep.store import Store

HERE = Path(__file__).resolve()


def write_index(cfg, names=("TCore.a",)):
    nodes = []
    for name in names:
        tags = {"ws": "A", "kind": "formalization", "depends": [], "consumer": "TCore.c"}
        nodes.append(Node(name=name, module=f"TBlueprint.A.{name.split('.')[-1].capitalize()}", type_pp="True", type_hash="h" + name,
                          sorry=True, axioms=["propext"], nonstandard_axioms=[], deps_actual=[], tags=tags))
    lock = {n.name: {"type_hash": n.type_hash} for n in nodes}
    compute(cfg, nodes, [], core_names=set(), claims={}, falsify={}, lock=lock, allow=[])
    cfg.nodes_json.write_text(json.dumps({"generated_at": "now", "git_sha": "abc", "nodes": [n.to_json() for n in nodes],
                                          "destinations": [], "problems": []}))
    cfg.lock_file.write_text(json.dumps(lock))
    return nodes


def run(args, capsys):
    rc = main(args)
    return rc, capsys.readouterr().out


def test_job_run_records_command_purpose_outputs_and_resources(cfg, monkeypatch):
    from lockstep.jobs import run_job, submit_run
    monkeypatch.setattr("lockstep.jobs.available_gib", lambda: 100.0)
    (cfg.root / "runs").mkdir()
    job = submit_run(cfg, command=[sys.executable, "-c", "open('runs/sub/out.txt','w').write('1')"], why="write a marker",
                     outputs=["runs/sub/out.txt"], agent="tester", node=None, memory_max="1G", queue=True)
    assert (cfg.root / "runs" / "sub").is_dir()
    assert job["kind"] == "run" and job["why"] == "write a marker" and json.loads(job["outputs"]) == ["runs/sub/out.txt"]
    done = run_job(cfg, job["id"])
    assert done["status"] == "done" and done["exit_code"] == 0 and done["seconds"] is not None
    result = json.loads(done["result_json"])
    assert result["outputs_present"] == ["runs/sub/out.txt"] and result["outputs_missing"] == []
    with Store(cfg) as st:
        kinds = [(e["kind"], json.loads(e["detail"])) for e in st.events_since(0) if e["kind"].startswith("job-")]
    assert kinds[0][0] == "job-submitted" and kinds[0][1]["why"] == "write a marker"
    assert kinds[-1][0] == "job-finished" and "peak_mib" in kinds[-1][1] and kinds[-1][1]["status"] == "done"


def test_job_run_refuses_scratch_outputs_and_missing_purpose(cfg, monkeypatch):
    from lockstep.jobs import submit_run
    monkeypatch.setattr("lockstep.jobs.available_gib", lambda: 100.0)
    with pytest.raises(CheckFailed, match="outside the project"):
        submit_run(cfg, command=["true"], why="x", outputs=["/tmp/scratch/out.json"], agent="tester", queue=True)
    with pytest.raises(CheckFailed, match="not under runs, data"):
        submit_run(cfg, command=["true"], why="x", outputs=["docs/out.json"], agent="tester", queue=True)
    with pytest.raises(CheckFailed, match="--why"):
        submit_run(cfg, command=["true"], why="  ", outputs=["runs/o"], agent="tester", queue=True)
    with pytest.raises(CheckFailed, match="--out"):
        submit_run(cfg, command=["true"], why="x", outputs=[], agent="tester", queue=True)


def test_job_run_enforces_memory_budget(cfg, monkeypatch):
    from lockstep.config import load_config
    from lockstep.jobs import submit_run
    toml = cfg.root / "harness" / "config.toml"
    toml.write_text(toml.read_text() + '\n[jobs]\nmemory_budget = "3G"\n')
    cfg2 = load_config(cfg.root)
    monkeypatch.setattr("lockstep.jobs.available_gib", lambda: 100.0)
    monkeypatch.setattr("lockstep.jobs.bus_available", lambda: True)
    monkeypatch.setattr("lockstep.jobs.spawn_runner", lambda cfg_, jid: None)
    first = submit_run(cfg2, command=["true"], why="first", outputs=["runs/a"], agent="tester", memory_max="2G")
    with Store(cfg2) as st:
        st.job_update(first["id"], status="running")
    with pytest.raises(CheckFailed, match="memory budget") as exc:
        submit_run(cfg2, command=["true"], why="second", outputs=["runs/b"], agent="tester", memory_max="2G")
    assert first["id"] in str(exc.value)
    small = submit_run(cfg2, command=["true"], why="third", outputs=["runs/c"], agent="tester", memory_max="1G")
    assert small["status"] == "queued"  # inserted queued; the runner flips it to running
    # a queued submission is accepted even when it does not fit now
    waiting = submit_run(cfg2, command=["true"], why="fourth", outputs=["runs/d"], agent="tester", memory_max="2G", queue=True)
    assert waiting["status"] == "queued"
    with pytest.raises(CheckFailed, match="exceeds the memory budget"):
        submit_run(cfg2, command=["true"], why="huge", outputs=["runs/e"], agent="tester", memory_max="9G", queue=True)


def test_claim_needs_why_and_release_needs_outcome_note(cfg, capsys):
    write_index(cfg)
    rc, out = run(["claim", "TCore.a"], capsys)
    assert rc != 0
    rc, out = run(["claim", "TCore.a", "--why", "prove it by induction"], capsys)
    assert rc == 0 and "prove it by induction" in out
    rc, out = run(["status"], capsys)
    assert "tester: prove it by induction" in out
    rc, out = run(["release", "TCore.a", "--outcome", "partial"], capsys)
    assert rc != 0
    rc, out = run(["release", "TCore.a", "--outcome", "partial", "--note", "base case done, step open"], capsys)
    assert rc == 0
    rc, out = run(["status"], capsys)
    assert "(partial" in out and "base case done" in out
    with Store(cfg) as st:
        att = st.attempts("TCore.a")
    assert att[-1]["outcome"] == "partial" and att[-1]["note"] == "base case done, step open"


def test_log_and_journal(cfg, capsys):
    write_index(cfg)
    rc, _ = run(["claim", "TCore.a", "--why", "try the window identity"], capsys)
    rc, out = run(["log", "the naive path lemma is false, switching to windows", "--node", "TCore.a"], capsys)
    assert rc == 0
    rc, out = run(["journal", "--since", "1h"], capsys)
    assert rc == 0 and "switching to windows" in out and "try the window identity" in out
    rc, out = run(["journal", "--since", "1h", "--kind", "log"], capsys)
    assert "switching to windows" in out and "try the window identity" not in out
    rc, out = run(["journal", "--since", "1h", "--agent", "nobody"], capsys)
    assert "nothing recorded" in out


def test_status_and_ready_warn_when_lock_has_unindexed_nodes(cfg, capsys):
    write_index(cfg)
    lock = json.loads(cfg.lock_file.read_text())
    lock["TCore.added_later"] = {"type_hash": "x"}
    cfg.lock_file.write_text(json.dumps(lock))
    rc, out = run(["status"], capsys)
    assert rc == 0 and "index is stale: 1 node(s)" in out and "added_later" in out
    rc, out = run(["ready"], capsys)
    assert "index is stale" in out


def test_status_lists_live_jobs(cfg, capsys, monkeypatch):
    from lockstep.jobs import submit_run
    write_index(cfg)
    monkeypatch.setattr("lockstep.jobs.available_gib", lambda: 100.0)
    job = submit_run(cfg, command=["sleep", "0"], why="a sweep for the table", outputs=["runs/t"], agent="tester", queue=True)
    rc, out = run(["status"], capsys)
    assert "jobs live: 1" in out and "a sweep for the table" in out and job["id"] in out


def test_hook_blocks_bare_nohup_and_systemd_run(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(HERE.parents[1])}

    def rc_for(command: str) -> int:
        return subprocess.run([sys.executable, "-m", "lockstep", "hook", "pre-bash"], input=json.dumps({"tool_input": {"command": command}}),
                              text=True, capture_output=True, cwd=tmp_path, env=env).returncode

    assert rc_for("nohup python3 scripts/sweep.py > runs/x.log &") == 2
    assert rc_for("systemd-run --user --scope -p MemoryMax=2G -- python3 x.py") == 2
    assert rc_for("tools/lockstep/bin/lockstep job run --why x --out runs/a -- python3 x.py") == 0
    assert rc_for("ls -la; echo nohup_is_not_a_word_boundary_here") == 0


def test_read_only_database_serves_reads_and_refuses_writes(cfg, capsys):
    """A sandboxed agent can read the shared state; writes fail with the fix spelled out."""
    import os
    from lockstep.errors import LockstepError
    write_index(cfg)
    with Store(cfg) as st:
        st.claim("TCore.a", "other-agent", ttl_hours=1, note="theirs")
    db = cfg.db_path
    os.chmod(db, 0o444)
    os.chmod(db.parent, 0o555)
    try:
        with Store(cfg) as st:
            assert st.readonly
            assert st.claims()["TCore.a"]["agent"] == "other-agent"
            with pytest.raises(LockstepError, match="read-only"):
                st.log("x", None, "me")
        rc, out = run(["status"], capsys)
        assert rc == 0 and "other-agent: theirs" in out
        rc, out = run(["journal", "--since", "1h", "--no-commits"], capsys)
        assert rc == 0 and "theirs" in out
        rc, out = run(["log", "trying to write from a sandbox"], capsys)
        assert rc == 2
    finally:
        os.chmod(db.parent, 0o755)
        os.chmod(db, 0o644)
    with Store(cfg) as st:
        assert not st.readonly


def test_state_home_from_config_is_inside_the_checkout(cfg, monkeypatch):
    from lockstep.config import load_config
    monkeypatch.delenv("LOCKSTEP_HOME", raising=False)
    toml = cfg.root / "harness" / "config.toml"
    toml.write_text(toml.read_text().replace('id = "testproj"', 'id = "testproj"\nstate_home = ".lockstep"'))
    cfg2 = load_config(cfg.root)
    assert cfg2.state_home == (cfg.root / ".lockstep" / "testproj").resolve()
    assert cfg2.db_path.parent == cfg2.state_home
    monkeypatch.setenv("LOCKSTEP_HOME", str(cfg.root / "elsewhere"))
    assert load_config(cfg.root).state_home == cfg.root / "elsewhere" / "testproj"


def test_job_run_queues_when_the_bus_is_unreachable(cfg, monkeypatch, capsys):
    from lockstep.jobs import submit_run
    monkeypatch.setattr("lockstep.jobs.available_gib", lambda: 100.0)
    monkeypatch.setattr("lockstep.jobs.bus_available", lambda: False)
    toml = cfg.root / "harness" / "config.toml"
    toml.write_text(toml.read_text().replace("guard = false", "guard = true"))
    from lockstep.config import load_config
    cfg2 = load_config(cfg.root)
    job = submit_run(cfg2, command=["true"], why="from a sandbox", outputs=["runs/s"], agent="agent:1")
    assert job["status"] == "queued" and job["queued_for_worker"]
    write_index(cfg2)
    rc, out = run(["job", "requeue", job["id"]], capsys)
    assert rc != 0  # still queued
    with Store(cfg2) as st:
        st.job_update(job["id"], status="failed", exit_code=1)
    rc, out = run(["job", "requeue", job["id"][:15]], capsys)
    assert rc == 0
    with Store(cfg2) as st:
        assert st.job(job["id"])["status"] == "queued"


def test_status_uses_live_claims_and_lists_claims_on_unindexed_nodes(cfg, capsys):
    from lockstep.errors import UsageError
    write_index(cfg)
    with Store(cfg) as st:
        st.claim("TCore.a", "agent-x", ttl_hours=1, note="prove it")
        st.claim("TCore.on_a_branch", "agent-y", ttl_hours=1, note="new node in a worktree")
    # the index was written before the claim; status must still show the claim, and the release after it
    rc, out = run(["status"], capsys)
    assert "agent-x: prove it" in out and "claimed" in out
    assert "not in this index" in out and "on_a_branch" in out and "agent-y" in out
    rc, out = run(["ready"], capsys)
    assert "TCore.a" not in out
    with Store(cfg) as st:
        st.release("TCore.a", "agent-x", outcome="partial", note="half")
    rc, out = run(["status"], capsys)
    assert "(partial" in out and "agent-x: prove it" not in out
    rc, out = run(["ready"], capsys)
    assert "TCore.a" in out
    with Store(cfg) as st:
        with pytest.raises(UsageError, match="unexpanded"):
            st.log("x", None, "agent:${SESSION_ID:-x}")


def test_forced_claim_keeps_its_purpose(cfg, capsys):
    write_index(cfg, names=("TCore.a", "TCore.b"))
    import json
    data = json.loads(cfg.nodes_json.read_text())
    for rec in data["nodes"]:
        if rec["name"] == "TCore.b":
            rec["ready"], rec["blocked_by"] = False, ["TCore.a"]
    cfg.nodes_json.write_text(json.dumps(data))
    rc, out = run(["claim", "TCore.b", "--why", "prove it anyway", "--force"], capsys)
    assert rc == 0
    with Store(cfg) as st:
        assert st.claims()["TCore.b"]["note"] == "prove it anyway"


def test_worktree_shares_the_primary_checkout_state(cfg, monkeypatch, tmp_path):
    from lockstep.config import load_config, primary_root
    monkeypatch.delenv("LOCKSTEP_HOME", raising=False)
    toml = cfg.root / "harness" / "config.toml"
    toml.write_text(toml.read_text().replace('id = "testproj"', 'id = "testproj"\nstate_home = ".lockstep"'))
    wt = tmp_path / "wt"
    shutil.copytree(cfg.root, wt, ignore=shutil.ignore_patterns(".git"))
    (wt / ".git").write_text(f"gitdir: {cfg.root}/.git/worktrees/wt\n")
    assert primary_root(wt) == cfg.root
    assert load_config(wt).state_home == (cfg.root / ".lockstep" / "testproj").resolve()
    assert load_config(cfg.root).state_home == (cfg.root / ".lockstep" / "testproj").resolve()


def test_worker_starts_queued_jobs_within_the_budget(cfg, monkeypatch):
    from lockstep.config import load_config
    from lockstep.jobs import submit_run, worker_start_fitting
    toml = cfg.root / "harness" / "config.toml"
    toml.write_text(toml.read_text() + '\n[jobs]\nmemory_budget = "5G"\n')
    cfg2 = load_config(cfg.root)
    monkeypatch.setattr("lockstep.jobs.available_gib", lambda: 100.0)
    a = submit_run(cfg2, command=["true"], why="a", outputs=["runs/a"], agent="t", memory_max="3G", queue=True)
    b = submit_run(cfg2, command=["true"], why="b", outputs=["runs/b"], agent="t", memory_max="3G", queue=True)
    c = submit_run(cfg2, command=["true"], why="c", outputs=["runs/c"], agent="t", memory_max="2G", queue=True)
    spawned = []
    started = worker_start_fitting(cfg2, spawn=lambda cfg_, jid: spawned.append(jid))
    # a (3G) fits, b (3G) would exceed 5G, c (2G) fits next to a
    assert started == [a["id"], c["id"]] and spawned == started
    with Store(cfg2) as st:
        assert st.job(b["id"])["status"] == "queued" and st.job(a["id"])["status"] == "starting"
    # queued submission is allowed even when it does not fit now
    big = submit_run(cfg2, command=["true"], why="d", outputs=["runs/d"], agent="t", memory_max="4G", queue=True)
    assert big["status"] == "queued"
