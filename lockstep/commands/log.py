"""The audit trail: `lockstep log` writes a free-text journal entry; `lockstep journal` shows the merged timeline
(claims, releases, jobs, falsifier runs, extracts, log entries, commits) so another agent can see what
was done, by whom, why, and with what resources."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from lockstep import EXIT_OK
from lockstep.context import Ctx
from lockstep.errors import UsageError
from lockstep.gitutil import git
from lockstep.output import Result, table
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def parse_since(spec: str | None, default: str = "24h") -> float:
    """`24h`, `3d`, `90m`, or a date `YYYY-MM-DD` -> epoch seconds."""
    spec = (spec or default).strip()
    m = re.fullmatch(r"(\d+)\s*([mhdw])", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return time.time() - n * {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[unit]
    try:
        return datetime.strptime(spec, "%Y-%m-%d").timestamp()
    except ValueError as exc:
        raise UsageError(f"--since takes 24h, 3d, 90m or YYYY-MM-DD, not {spec!r}") from exc


def _detail(kind: str, raw: str | None) -> str:
    if raw is None:
        return ""
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)
    if not isinstance(d, dict):
        return str(d)
    if kind == "release":
        return f"{d.get('outcome')}" + (f": {d.get('note')}" if d.get("note") else "")
    if kind == "job-submitted":
        cap = f" cap {d['memory_max']}" if d.get("memory_max") else ""
        return f"job {d.get('id')} {d.get('kind')}{cap}" + (f": {d.get('why')}" if d.get("why") else "")
    if kind == "job-finished":
        extra = ""
        if d.get("seconds") is not None:
            extra += f" {d['seconds']:.0f}s"
        if d.get("peak_mib"):
            extra += f" peak {d['peak_mib']} MiB"
        return f"job {d.get('id')} {d.get('status') or ('ok' if d.get('rc') == 0 else 'failed')} rc={d.get('rc')}{extra}"
    if kind == "job-killed":
        return f"job {d.get('id')} killed"
    if kind == "log":
        return str(d.get("text", ""))
    if kind == "falsify":
        return f"falsifier {d.get('result')} (rc={d.get('rc')})"
    if kind == "extract":
        scope = "targeted " + ",".join(d.get("imports") or []) if d.get("imports") else "full"
        return f"extract {scope}: {d.get('seconds', 0):.0f}s, peak {d.get('peak_mib')} MiB, cap {d.get('memory_max')}"
    if kind == "attempt":
        return f"{d.get('outcome')}" + (f": {d.get('note')}" if d.get("note") else "")
    return ", ".join(f"{k}={v}" for k, v in d.items())


def run_log(ctx: Ctx, text: str, node: str | None = None) -> Result:
    cfg = ctx.cfg
    if node:
        from lockstep.nodes import find_node
        node = find_node(cfg, node).name
    if not text.strip():
        raise UsageError("nothing to log")
    with Store(cfg) as st:
        st.log(text.strip(), node, ctx.agent)
    return Result(EXIT_OK, {"logged": text.strip(), "node": node, "agent": ctx.agent}, [f"logged for {ctx.agent}" + (f" on {node}" if node else "")])


def timeline(cfg, since: float, agent: str | None = None, node: str | None = None, kind: str | None = None,
             commits: bool = True) -> list[dict]:
    with Store(cfg) as st:
        events = [dict(e) for e in st.events_since(since)]
    rows = []
    for e in events:
        rows.append({"at": e["at"], "agent": e.get("agent") or "", "kind": e["kind"], "node": e.get("node") or "",
                     "text": _detail(e["kind"], e.get("detail"))})
    if commits:
        try:
            out = git(cfg, "log", f"--since=@{int(since)}", "--format=%h%x09%at%x09%an%x09%s", check=False)
        except (RuntimeError, OSError):
            out = ""
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                rows.append({"at": float(parts[1]), "agent": parts[2], "kind": "commit", "node": "", "text": f"{parts[0]} {parts[3]}"})
    if agent:
        rows = [r for r in rows if agent in r["agent"]]
    if node:
        rows = [r for r in rows if r["node"] == node or r["node"].endswith("." + node) or node in r["text"]]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    rows.sort(key=lambda r: r["at"])
    return rows


def run_journal(ctx: Ctx, since: str | None = None, agent: str | None = None, node: str | None = None,
                kind: str | None = None, limit: int = 200, no_commits: bool = False) -> Result:
    cfg = ctx.cfg
    start = parse_since(since)
    rows = timeline(cfg, start, agent=agent, node=node, kind=kind, commits=not no_commits)
    rows = rows[-limit:]
    res = Result(EXIT_OK, {"since": start, "entries": rows})
    if not rows:
        res.line(f"nothing recorded since {time.strftime('%Y-%m-%d %H:%M', time.localtime(start))}")
        return res
    def short(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1] + "…"
    res.human.extend(table([[time.strftime("%m-%d %H:%M", time.localtime(r["at"])), short(r["agent"], 26), r["kind"], short(r["node"].rsplit(".", 1)[-1] if r["node"] else "", 34), short(r["text"], 100)] for r in rows],
                           ["when", "agent", "kind", "node", "what"]))
    return res


command(CommandSpec(name="log", cli=["log"], help="Journal entry: what you are doing and why (shown by `lockstep journal`).", run=run_log,
                    args=[Arg("text", "the entry", positional=True, required=True), Arg("node", "node it concerns, if any")]))
command(CommandSpec(name="journal", cli=["journal"],
                    help="Timeline of claims, releases, jobs, falsifier runs, extracts, log entries and commits: what every agent did, why, and with what resources.",
                    run=run_journal,
                    args=[Arg("since", "24h (default), 3d, 90m, or YYYY-MM-DD"), Arg("agent", "only agents whose id contains this"),
                          Arg("node", "only this node (short name ok)"), Arg("kind", "only this event kind (claim, release, log, job-submitted, job-finished, falsify, extract, commit, ...)"),
                          Arg("limit", "most recent entries to show", "int", default=200), Arg("no_commits", "leave git commits out", "bool")]))
