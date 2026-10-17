from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from lockstep.build import BuildResult, run_guarded
from lockstep.errors import CheckFailed
from lockstep.storage import StorageTarget, exclusive_storage_maintenance
from lockstep.store import Store


def _lease(cfg, key: str, *, job_id=None) -> str:
    ident = uuid.uuid4().hex
    with Store(cfg) as st:
        st.conn.execute(
            "INSERT INTO resource_leases(id,job_id,pid,process_start,memory_gib,lock_key,created_at) VALUES (?,?,?,?,?,?,?)",
            (ident, job_id, os.getpid(), "test", 0.0, key, time.time()),
        )
    return ident


def _job(cfg, ident: str, status: str, lock_key: str | None, scope_unit: str | None = None) -> None:
    with Store(cfg) as st:
        st.conn.execute(
            "INSERT INTO jobs(id,kind,argv,cwd,status,created_at,lock_key,scope_unit) VALUES (?,?,?,?,?,?,?,?)",
            (ident, "run", "[]", str(cfg.root), status, time.time(), lock_key, scope_unit),
        )


def test_maintenance_conflicts_with_null_owner_direct_lease(cfg):
    key = f"worktree:{cfg.root.resolve()}"
    _lease(cfg, key)
    with pytest.raises(CheckFailed, match="storage reservation is occupied"):
        with exclusive_storage_maintenance(cfg, [cfg.root], reason="test"):
            pass


def test_exact_root_conflicts_even_when_parallel_capacity_remains(cfg, tmp_path):
    cfg.raw["jobs"] = {"max_parallel_builds": 2}
    _lease(cfg, f"worktree:{cfg.root.resolve()}")
    with pytest.raises(CheckFailed, match="storage reservation is occupied"):
        with exclusive_storage_maintenance(cfg, [cfg.root], reason="test"):
            pass


def test_direct_guarded_command_honors_exact_maintenance_lease(monkeypatch, cfg, tmp_path):
    cfg.raw["jobs"] = {"max_parallel_builds": 2}
    _lease(cfg, f"worktree:{cfg.root.resolve()}")
    monkeypatch.setattr(
        "lockstep.build._run_guarded",
        lambda *_args, **_kwargs: pytest.fail("occupied direct command must not launch"),
    )
    result = run_guarded(
        cfg, [cfg.lake, "build", "TCore"], log_path=tmp_path / "build.log", budget_min=1, force=True
    )
    assert result.refused and "storage reservation is occupied" in result.refused


def test_lake_build_cache_env_and_lock_are_atomic_and_lake_specific(monkeypatch, cfg, tmp_path):
    cache = tmp_path / "native-cache"
    cfg.raw.setdefault("build", {})["artifact_cache"] = {"enabled": True, "root": str(cache)}
    observations = []

    def fake(_cfg, argv, **kwargs):
        with Store(cfg) as st:
            keys = {row[0] for row in st.conn.execute("SELECT lock_key FROM resource_leases")}
        observations.append((argv, kwargs.get("env_extra") or {}, keys))
        return BuildResult(0, 0.1, 0, str(kwargs["log_path"]), argv)

    monkeypatch.setattr("lockstep.build._run_guarded", fake)
    run_guarded(cfg, [cfg.lake, "build", "TCore"], log_path=tmp_path / "lake.log", budget_min=1, force=True)
    run_guarded(cfg, ["python3", "-V"], log_path=tmp_path / "python.log", budget_min=1, force=True)

    lake, ordinary = observations
    assert lake[1]["LAKE_ARTIFACT_CACHE"] == "true"
    assert lake[1]["LAKE_CACHE_DIR"] == str(cache)
    assert f"worktree:{cfg.root.resolve()}" in lake[2]
    assert f"artifact-cache:{cache.resolve()}" in lake[2]
    assert "LAKE_ARTIFACT_CACHE" not in ordinary[1]
    assert not any(key.startswith("artifact-cache:") for key in ordinary[2])


def test_other_root_uses_global_capacity_and_multi_root_is_one_owner(cfg, tmp_path):
    other = tmp_path / "other"
    third = tmp_path / "third"
    other.mkdir()
    third.mkdir()
    _lease(cfg, f"worktree:{other.resolve()}")
    with pytest.raises(CheckFailed, match="concurrency reservation"):
        with exclusive_storage_maintenance(cfg, [cfg.root], reason="test"):
            pass

    cfg.raw["jobs"] = {"max_parallel_builds": 2}
    with exclusive_storage_maintenance(cfg, [cfg.root, third], reason="test") as reservation:
        assert len(reservation.lease_ids) == 2
        with Store(cfg) as st:
            rows = [dict(row) for row in st.conn.execute(
                "SELECT job_id,lock_key FROM resource_leases WHERE id IN (?,?)", reservation.lease_ids
            )]
        assert len({row["job_id"] for row in rows}) == 1
        assert {row["lock_key"] for row in rows} == {
            f"worktree:{cfg.root.resolve()}", f"worktree:{third.resolve()}"
        }


def test_generic_storage_target_has_exact_exclusion(cfg, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    target = StorageTarget("artifact-cache", cache)
    with exclusive_storage_maintenance(cfg, targets=[target], reason="first"):
        with pytest.raises(CheckFailed, match="storage reservation is occupied"):
            with exclusive_storage_maintenance(cfg, targets=[target], reason="second"):
                pass


def test_running_owner_can_add_other_root_and_inherits_scope(cfg, tmp_path):
    cfg.raw["jobs"] = {"max_parallel_builds": 1}
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    ident = "owner"
    _job(cfg, ident, "running", f"worktree:{first.resolve()}", "owner.scope")
    with exclusive_storage_maintenance(cfg, [second], reason="nested", owner_job_id=ident) as reservation:
        assert len(reservation.lease_ids) == 1
        with Store(cfg) as st:
            row = dict(st.conn.execute("SELECT * FROM resource_leases WHERE id=?", reservation.lease_ids).fetchone())
        assert row["job_id"] == ident
        assert row["scope_unit"] == "owner.scope"


@pytest.mark.parametrize("status", ["starting", "unknown_execution"])
def test_nonrunning_owner_cannot_authorize_supplemental_lease(cfg, tmp_path, status):
    other = tmp_path / status
    other.mkdir()
    _job(cfg, "owner", status, None)
    with pytest.raises(CheckFailed, match="enclosing job is not running"):
        with exclusive_storage_maintenance(cfg, [other], reason="test", owner_job_id="owner"):
            pass
