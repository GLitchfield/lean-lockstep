"""Atomic reservations for maintenance and writers of shared build storage."""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from lockstep.errors import CheckFailed
from lockstep.store import Store


LIVE_JOB_STATUSES = ("starting", "running", "unknown_execution")


@dataclass(frozen=True)
class StorageTarget:
    """One exact filesystem-backed lock namespace.

    ``worktree`` targets participate in ``max_parallel_builds``.  Other kinds,
    such as ``artifact-cache``, provide exact-key exclusion without consuming a
    second global build slot when held by the same operation.
    """

    kind: str
    path: Path

    @property
    def key(self) -> str:
        if not self.kind or ":" in self.kind:
            raise CheckFailed(f"invalid storage target kind {self.kind!r}")
        return f"{self.kind}:{self.path.resolve()}"


@dataclass(frozen=True)
class StorageReservation:
    group_id: str
    lease_ids: tuple[str, ...]
    lock_keys: tuple[str, ...]
    owner_job_id: str | None
    reason: str


def worktree_target(root: Path) -> StorageTarget:
    return StorageTarget("worktree", root)


def _targets(roots: Iterable[Path], targets: Iterable[StorageTarget]) -> tuple[StorageTarget, ...]:
    by_key: dict[str, StorageTarget] = {}
    for target in (*[worktree_target(root) for root in roots], *targets):
        by_key[target.key] = target
    if not by_key:
        raise CheckFailed("a storage reservation needs at least one target")
    return tuple(by_key[key] for key in sorted(by_key))


def _worktree_owners(jobs: list[dict], leases: list[dict], *, exclude_job_id: str | None = None) -> set[tuple[str, str]]:
    """Count operations, not keys: a multi-root reservation occupies one slot."""
    live_ids = {str(row["id"]) for row in jobs}
    owners: set[tuple[str, str]] = set()
    for row in jobs:
        if (exclude_job_id is None or row["id"] != exclude_job_id) and str(row.get("lock_key") or "").startswith("worktree:"):
            owners.add(("job", str(row["id"])))
    for row in leases:
        if not str(row.get("lock_key") or "").startswith("worktree:"):
            continue
        job_id = row.get("job_id")
        if exclude_job_id is not None and job_id == exclude_job_id:
            continue
        if job_id and (str(job_id) in live_ids or str(job_id).startswith("storage:")):
            owners.add(("job", str(job_id)))
        else:
            owners.add(("lease", str(row["id"])))
    return owners


def reserve_storage_in_transaction(
    st: Store,
    cfg,
    *,
    roots: Iterable[Path] = (),
    targets: Iterable[StorageTarget] = (),
    reason: str,
    owner_job_id: str | None = None,
    memory_gib: float = 0.0,
) -> StorageReservation:
    """Reserve targets using the caller's active ``BEGIN IMMEDIATE`` transaction.

    Exact-key conflicts and the global worktree-writer limit are decided from
    one SQLite snapshot.  Filesystem work must start only after that transaction
    commits.  An enclosing job already holding a requested key supplies the
    reservation; otherwise zero-memory supplemental leases are attached to it.
    """
    requested = _targets(roots, targets)
    requested_keys = tuple(target.key for target in requested)
    jobs = [dict(row) for row in st.conn.execute(
        "SELECT * FROM jobs WHERE status IN ('starting','running','unknown_execution')"
    )]
    leases = [dict(row) for row in st.conn.execute("SELECT * FROM resource_leases")]
    owner = next((row for row in jobs if row["id"] == owner_job_id and row["status"] == "running"), None)
    if owner_job_id and owner is None:
        raise CheckFailed("enclosing job is not running")

    owner_keys = set()
    if owner:
        if owner.get("lock_key"):
            owner_keys.add(str(owner["lock_key"]))
        owner_keys.update(str(row["lock_key"]) for row in leases if row.get("job_id") == owner_job_id and row.get("lock_key"))

    other_job_keys = {
        str(row["lock_key"]) for row in jobs
        if (owner_job_id is None or row["id"] != owner_job_id) and row.get("lock_key")
    }
    other_lease_keys = {
        str(row["lock_key"]) for row in leases
        if (owner_job_id is None or row.get("job_id") != owner_job_id) and row.get("lock_key")
    }
    conflicts = sorted(set(requested_keys) & (other_job_keys | other_lease_keys))
    if conflicts:
        raise CheckFailed("storage reservation is occupied: " + ", ".join(conflicts))

    missing = [target for target in requested if target.key not in owner_keys]
    if any(target.kind == "worktree" for target in missing):
        occupied = _worktree_owners(jobs, leases, exclude_job_id=owner_job_id)
        if len(occupied) >= cfg.max_parallel_builds:
            raise CheckFailed("worktree/build concurrency reservation is occupied")

    group_id = owner_job_id or f"storage:{uuid.uuid4().hex}"
    lease_ids: list[str] = []
    process_start = None
    try:
        from lockstep.jobs import process_identity

        process_start = process_identity(os.getpid())
    except (ImportError, OSError):
        pass
    for index, target in enumerate(missing):
        lease_id = uuid.uuid4().hex
        st.conn.execute(
            "INSERT INTO resource_leases(id,job_id,pid,process_start,memory_gib,lock_key,created_at,scope_unit) VALUES (?,?,?,?,?,?,?,?)",
            (
                lease_id,
                group_id,
                os.getpid(),
                process_start,
                memory_gib if index == 0 else 0.0,
                target.key,
                time.time(),
                owner.get("scope_unit") if owner else None,
            ),
        )
        lease_ids.append(lease_id)
    return StorageReservation(group_id, tuple(lease_ids), requested_keys, owner_job_id, reason)


def release_storage_reservation(cfg, reservation: StorageReservation) -> None:
    if not reservation.lease_ids:
        return
    with Store(cfg) as st:
        placeholders = ",".join("?" for _ in reservation.lease_ids)
        st.conn.execute(f"DELETE FROM resource_leases WHERE id IN ({placeholders})", reservation.lease_ids)


@contextmanager
def exclusive_storage_maintenance(
    cfg,
    roots: Iterable[Path] = (),
    *,
    targets: Iterable[StorageTarget] = (),
    reason: str,
    owner_job_id: str | None = None,
) -> Iterator[StorageReservation]:
    """Hold exact storage targets while maintenance runs, without an open DB transaction."""
    with Store(cfg) as st:
        st.conn.execute("BEGIN IMMEDIATE")
        reservation = reserve_storage_in_transaction(
            st, cfg, roots=roots, targets=targets, reason=reason, owner_job_id=owner_job_id
        )
    try:
        yield reservation
    finally:
        release_storage_reservation(cfg, reservation)
