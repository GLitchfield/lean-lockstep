from __future__ import annotations

import time

from lockstep import EXIT_CHECK_FAILED, EXIT_OK
from lockstep.context import Ctx
from lockstep.errors import UsageError
from lockstep.lock import read_lock, write_lock
from lockstep.nodes import load_nodes_json, nodes_from_json
from lockstep.output import Result, table
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def run_refresh(ctx: Ctx, reason: str, approved_by: str | None = None, name: list[str] | None = None, all_: bool = False) -> Result:
    """Re-approve lock hashes whose pretty-printed statement is unchanged (toolchain bumps change Expr hashes)."""
    cfg = ctx.cfg
    if not all_ and not name:
        raise UsageError("give node names or --all")
    data = load_nodes_json(cfg)
    nodes = nodes_from_json(data)
    lock = read_lock(cfg.lock_file)
    rows = []
    refreshed = []
    for n in nodes:
        if name and n.name not in name and n.short not in name:
            continue
        entry = lock.get(n.name)
        if entry is None:
            rows.append([n.name, "no lock entry", "skipped"])
            continue
        if entry.get("type_hash") == n.type_hash:
            rows.append([n.name, "hash unchanged", "-"])
            continue
        if entry.get("type_pp") != n.type_pp:
            rows.append([n.name, "STATEMENT TEXT CHANGED", "refused (use `lockstep node restate`)"])
            continue
        entry["type_hash"] = n.type_hash
        entry["refreshed_at"] = time.strftime("%Y-%m-%d")
        entry["refreshed_by"] = approved_by or ctx.agent
        entry["refresh_reason"] = reason
        refreshed.append(n.name)
        rows.append([n.name, "same statement, new hash", "refreshed"])
    if refreshed:
        write_lock(cfg.lock_file, lock)
        with Store(cfg) as st:
            st.event("lock-refreshed", None, ctx.agent, {"nodes": refreshed, "reason": reason})
    res = Result(EXIT_OK if not any(r[2].startswith("refused") for r in rows) else EXIT_CHECK_FAILED,
                 {"refreshed": refreshed, "rows": rows}, table(rows, ["node", "state", "action"]))
    res.line(f"{len(refreshed)} lock entr{'y' if len(refreshed) == 1 else 'ies'} refreshed" + (f"; run `lockstep extract` to clear the lock problems" if refreshed else ""))
    return res


command(CommandSpec(name="lock_refresh", cli=["lock", "refresh"],
                    help="Re-approve lock hashes for nodes whose statement text is unchanged but whose hash moved (toolchain bump).",
                    run=run_refresh,
                    args=[Arg("reason", "why the hashes moved, e.g. 'toolchain v4.30'", required=True), Arg("approved_by", "who approves"),
                          Arg("name", "node names (repeatable / comma-separated)", "list"), Arg("all_", "every node with an unchanged statement", "bool")]))
