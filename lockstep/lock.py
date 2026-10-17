"""Statement lock: node name -> approved type hash."""

from __future__ import annotations

import json
import time
from pathlib import Path


def read_lock(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def write_lock(path: Path, lock: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(lock.items())), indent=2, ensure_ascii=False) + "\n")


def lock_entry(type_hash: str, type_pp: str, approved_by: str, kind: str, ws: str) -> dict:
    return {
        "type_hash": type_hash,
        "type_pp": type_pp,
        "approved_by": approved_by,
        "approved_at": time.strftime("%Y-%m-%d"),
        "kind": kind,
        "ws": ws,
    }


def check_lock(lock: dict[str, dict], nodes: list) -> list[str]:
    """Problems: nodes missing from the lock or whose hash changed."""
    problems = []
    for n in nodes:
        entry = lock.get(n.name)
        if entry is None:
            problems.append(f"{n.name}: not in statements.lock (approve with `lockstep node add`/`lockstep node restate`)")
        elif entry.get("type_hash") != n.type_hash:
            problems.append(f"{n.name}: statement changed by hand (lock {entry.get('type_hash')}, now {n.type_hash}); use `lockstep node restate`")
    return problems


def statement_changes(old: dict[str, dict], new: dict[str, dict]) -> dict[str, str]:
    """Approved statements that differ between two locks, each mapped to the log that must
    record it: `retired.md` for a statement negated by `lockstep node retire --as-refutation`,
    `restatements.md` for `lockstep node restate` or a removed entry. New entries (`lockstep node add`)
    and metadata (`lockstep close` adds `merged_module`) change no approved statement."""
    changes = {}
    for name, entry in old.items():
        now = new.get(name)
        if now is None:
            changes[name] = "restatements.md"
        elif now.get("type_hash") != entry.get("type_hash"):
            changes[name] = "retired.md" if now.get("kind") == "refutation" else "restatements.md"
    return changes


if __name__ == "__main__":
    # CI: `python3 -m lockstep.lock BASE_LOCK HEAD_LOCK` prints the logs the change must touch.
    import sys

    base, head = (json.loads(Path(p).read_text() or "{}") for p in sys.argv[1:3])
    print("\n".join(sorted(set(statement_changes(base, head).values()))))
