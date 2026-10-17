from __future__ import annotations

import time
from datetime import datetime

from lockstep import EXIT_OK
from lockstep.context import Ctx
from lockstep.lock import read_lock
from lockstep.nodes import compute, days_open, dests_from_json, load_nodes_json, nodes_from_json
from lockstep.output import Result, table
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def stale_lines(cfg, nodes) -> list[str]:
    """Warn when active statement locks are absent from the extracted index."""
    try:
        lock = read_lock(cfg.lock_file)
    except ValueError:
        return []
    indexed = {n.name for n in nodes}
    missing = sorted(
        name
        for name, entry in lock.items()
        if name not in indexed and not (isinstance(entry, dict) and entry.get("retired"))
    )
    if not missing:
        return []
    # lock entries of nodes that were closed and merged into the core stay in the lock;
    # they are indexed declarations, not missing nodes
    try:
        import json
        core = {r.get("name") for r in json.loads(cfg.index_json.read_text())} if cfg.index_json.exists() else set()
    except (OSError, ValueError):
        core = set()
    missing = [m for m in missing if m not in core]
    if not missing:
        return []
    shown = ", ".join(m.rsplit(".", 1)[-1] for m in missing[:6]) + ("…" if len(missing) > 6 else "")
    return [f"index is stale: {len(missing)} node(s) in statements.lock are not indexed ({shown}); run `lockstep extract`"]


def load_live(cfg, claims: dict[str, dict]):
    """nodes.json with status, readiness and falsifier state recomputed from the live claims table,
    the falsifier results and the lock, so `lockstep status`/`lockstep ready` do not show a claim that ended
    (or a falsifier that passed) after the last extract."""
    import json
    data = load_nodes_json(cfg)
    nodes = nodes_from_json(data)
    dests = dests_from_json(data)
    try:
        core_names = {r.get("name") for r in json.loads(cfg.index_json.read_text())} if cfg.index_json.exists() else set()
    except (OSError, ValueError):
        core_names = set()
    if core_names:
        compute(cfg, nodes, dests, core_names, claims=claims)
    return data, nodes, dests


def _short(text, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def who(n, latest: dict, now: float) -> str:
    """The claimed-by cell: agent and intent, or what happened to the last attempt."""
    if n.claim:
        why = n.claim.get("note")
        return _short(n.claim.get("agent", "") + (f": {why}" if why else ""), 70)
    a = latest.get(n.name)
    if a and n.status in ("open", "proposed") and (now - (a.get("ended_at") or 0)) < 14 * 86400:
        when = time.strftime("%m-%d", time.localtime(a.get("ended_at") or now))
        return _short(f"({a.get('outcome')} {when} {a.get('agent') or '?'}" + (f": {a.get('note')}" if a.get("note") else "") + ")", 70)
    return ""


def burn_down(cfg, weeks: int = 8) -> list[dict]:
    with Store(cfg) as st:
        since = time.time() - weeks * 7 * 86400
        attempts = st.attempts(since=since)
        events = st.events_since(since)
    buckets: dict[str, dict[str, int]] = {}

    def week(ts: float) -> str:
        iso = datetime.fromtimestamp(ts).isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    for a in attempts:
        b = buckets.setdefault(week(a["started_at"] or time.time()), {"attempts": 0, "closed": 0, "refuted": 0, "retired": 0})
        b["attempts"] += 1
    for e in events:
        if e["kind"] in ("closed", "refuted", "retired"):
            b = buckets.setdefault(week(e["at"]), {"attempts": 0, "closed": 0, "refuted": 0, "retired": 0})
            b[e["kind"]] += 1
    return [{"week": w, **c} for w, c in sorted(buckets.items())]


def run(ctx: Ctx, brief: bool = False, ws: str | None = None) -> Result:
    cfg = ctx.cfg
    with Store(cfg) as st:
        claims = st.claims()
        latest = st.latest_attempts()
        live = st.live_jobs()
    data, nodes, dests = load_live(cfg, claims)
    stale = stale_lines(cfg, nodes)
    for n in nodes:
        n.claim = claims.get(n.name)
        if n.claim and n.status == "open":
            n.status = "claimed"
    indexed = {n.name for n in nodes}
    elsewhere = {k: v for k, v in claims.items() if k not in indexed}
    if ws:
        nodes = [n for n in nodes if n.ws == ws]
    res = Result()
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n.status] = counts.get(n.status, 0) + 1
    res.line(f"frontier @ {data.get('git_sha', '?')[:12]} ({data.get('generated_at', '?')}): " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no nodes"))
    for line in stale:
        res.line(line)
    problems = data.get("problems", []) + data.get("lock_problems", []) + data.get("duplicate_problems", [])
    if problems:
        res.line(f"{len(problems)} problem(s) reported by `lockstep extract` (run `lockstep extract --check`)")
    rows = []
    now = time.time()
    for w in cfg.workstreams:
        group = sorted((n for n in nodes if n.ws == w), key=lambda x: x.name)
        for n in group:
            f = (n.falsify or {}).get("result", "-") if n.falsifier else "-"
            rows.append([w, n.name, n.kind, n.status, "yes" if n.ready else ("blocked" if n.blocked_by else ""),
                         who(n, latest, now), days_open(n.opened), f])
    for n in (x for x in nodes if x.ws not in cfg.workstreams):
        rows.append(["?", n.name, n.kind, n.status, "", "", "", "tags: " + "; ".join(n.tag_errors)])
    if brief:
        rows = [r for r in rows if r[3] not in ("closed", "merged", "retired", "refuted")][:18]
    res.human.extend(table(rows, ["ws", "node", "kind", "status", "ready", "claimed by / last attempt", "days", "falsifier"]))
    if elsewhere:
        res.line("")
        res.line(f"claims on {len(elsewhere)} node(s) not in this index (added on another branch or worktree; they appear here once merged and extracted):")
        res.human.extend(table([[k.rsplit(".", 1)[-1], _short(v.get("agent"), 40), _short(v.get("note"), 70)] for k, v in sorted(elsewhere.items())],
                               ["node", "agent", "why"]))
    if live:
        from lockstep.commands.jobs import JOB_HEADERS, job_rows
        res.line("")
        res.line(f"jobs live: {len(live)} (`lockstep job list`, `lockstep journal` for the trail)")
        res.human.extend(table(job_rows(live), JOB_HEADERS))
    if not brief:
        bad = [d for d in dests if d.sorry or d.uncovered_axioms]
        res.line("")
        res.line(f"destinations: {len(dests)} audited" + (f", {len(bad)} with sorry or uncovered axioms: " + ", ".join(d.name for d in bad) if bad else ", all clean"))
        bd = burn_down(cfg)
        if bd:
            res.line("")
            res.human.extend(table([[b["week"], b["attempts"], b["closed"], b["refuted"], b["retired"]] for b in bd], ["week", "attempts", "closed", "refuted", "retired"]))
    res.payload = {"generated_at": data.get("generated_at"), "git_sha": data.get("git_sha"), "counts": counts, "stale": stale,
                   "nodes": [n.to_json() | {"claim": n.claim, "days_open": days_open(n.opened), "last_attempt": latest.get(n.name)} for n in nodes],
                   "destinations": [d.to_json() for d in dests], "problems": problems, "jobs_live": live,
                   "claims_elsewhere": elsewhere,
                   "burn_down": None if brief else burn_down(cfg)}
    res.exit_code = EXIT_OK
    return res


command(
    CommandSpec(
        name="status",
        cli=["status"],
        help="Frontier grouped by workstream, then burn-down.",
        run=run,
        args=[Arg("brief", "at most ~25 lines, for the SessionStart hook", "bool"), Arg("ws", "only this workstream")],
    )
)
