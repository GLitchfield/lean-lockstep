from __future__ import annotations

import time

from lockstep import EXIT_CHECK_FAILED, EXIT_OK
from lockstep.context import Ctx
from lockstep.errors import CheckFailed
from lockstep.nodes import find_node, load_nodes_json, nodes_from_json
from lockstep.output import Result
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def run_claim(ctx: Ctx, name: str, why: str | None = None, agent: str | None = None, note: str | None = None, force: bool = False) -> Result:
    cfg = ctx.cfg
    agent = agent or ctx.agent
    why = (why or note or "").strip() or None
    if cfg.claims_require_why and not why:
        raise CheckFailed("--why is required: say what you intend to do with the node (shown in `lockstep status`)")
    n = find_node(cfg, name)
    with Store(cfg) as st:
        existing = st.claims().get(n.name)
        if existing and existing["agent"] != agent:
            raise CheckFailed(f"{n.name} is claimed by {existing['agent']} until {time.strftime('%Y-%m-%d %H:%M', time.localtime(existing['expires_at']))}")
        if n.status == "proposed" and n.kind == "conjecture":
            raise CheckFailed(f"{n.name} is a conjecture whose falsifier has not passed; run `lockstep falsify {n.short}` first")
        if not n.ready and not existing:
            reason = "blocked by " + ", ".join(n.blocked_by) if n.blocked_by else f"status {n.status}"
            if not force:
                raise CheckFailed(f"{n.name} is not ready ({reason}); use --force to claim anyway")
            st.event("claim-forced", n.name, agent, {"reason": reason, "why": why})
        claim = st.claim(n.name, agent, cfg.claim_ttl_hours, why)
    return Result(EXIT_OK, {"claim": claim}, [f"claimed {n.name} for {agent} until {time.strftime('%Y-%m-%d %H:%M', time.localtime(claim['expires_at']))}"
                                              + (f": {why}" if why else "")])


OUTCOMES = ["closed", "partial", "blocked", "abandoned", "refuted"]


def run_release(ctx: Ctx, name: str, outcome: str, note: str | None = None, agent: str | None = None) -> Result:
    cfg = ctx.cfg
    n = find_node(cfg, name)
    note = (note or "").strip() or None
    # releasing a node that is already sorry-free is not an abandoned attempt
    if outcome in ("abandoned", "partial") and n.status in ("closed", "merged"):
        outcome = "closed"
    if outcome in ("partial", "blocked", "abandoned") and not note:
        raise CheckFailed(f"--note is required with --outcome {outcome}: say what was done and what stopped you, for the next agent")
    with Store(cfg) as st:
        ok = st.release(n.name, agent or ctx.agent, outcome=outcome, note=note)
    if not ok:
        return Result(EXIT_CHECK_FAILED, {"released": False}, [f"{n.name} was not claimed"])
    return Result(EXIT_OK, {"released": True, "outcome": outcome, "note": note}, [f"released {n.name} ({outcome}" + (f": {note}" if note else "") + ")"])


command(CommandSpec(name="claim", cli=["claim"], help="Claim a ready node for this agent (12 h by default); --why records the intent shown in `lockstep status`.", run=run_claim,
                    args=[Arg("name", "node name", positional=True, required=True), Arg("why", "what you intend to do (required unless [claims] require_why = false)"),
                          Arg("agent", "agent id (default: LOCKSTEP_AGENT)"), Arg("note", "alias of --why"), Arg("force", "claim a node that is not ready (logged)", "bool")]))
command(CommandSpec(name="release", cli=["release"], help="Release a claim with an outcome; partial/blocked/abandoned need --note (what was done, what stopped you).", run=run_release,
                    args=[Arg("name", "node name", positional=True, required=True),
                          Arg("outcome", "closed | partial | blocked | abandoned | refuted", required=True, choices=OUTCOMES),
                          Arg("note", "what was done and what remains"), Arg("agent", "agent id")]))
