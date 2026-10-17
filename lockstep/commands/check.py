from __future__ import annotations

import time

from lockstep import EXIT_LEAN, EXIT_OK
from lockstep.build import run_guarded
from lockstep.context import Ctx
from lockstep.nodes import find_node
from lockstep.output import Result
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def run(ctx: Ctx, name: str, budget: int | None = None, async_: bool = False, force: bool = False,
        targeted: bool = False) -> Result:
    cfg = ctx.cfg
    n = find_node(cfg, name)
    if async_:
        from lockstep.jobs import submit
        argv = ["check", n.name] + ([f"--budget={budget}"] if budget else [])
        if targeted:
            argv.append("--targeted")
        job = submit(cfg, kind="check", node=n.name, argv=argv, agent=ctx.agent, budget_min=budget)
        return Result(EXIT_OK, {"job": job}, [f"submitted job {job['id']}"])
    log = cfg.build_log_dir / f"check-{time.strftime('%Y%m%d-%H%M%S')}-{n.short}.log"
    started = time.time()
    build = run_guarded(cfg, [cfg.lake, "build", n.module], log_path=log, budget_min=budget, force=force)
    res = Result()
    res.line(f"lake build {n.module}: {build.summary()}")
    if not build.ok:
        res.human.extend(build.tail)
        res.exit_code = EXIT_LEAN
        res.payload = {"node": n.name, "build": build.__dict__}
        with Store(cfg) as st:
            st.event("check-failed", n.name, ctx.agent, {"seconds": build.seconds})
        return res
    from lockstep.commands.extract import run_extract
    from lockstep.commands.node import targeted_extract
    from lockstep.errors import Refused

    if targeted:
        payload, index, problems = targeted_extract(ctx, n.module)
    else:
        try:
            payload, index, problems = run_extract(ctx, force=force)
        except Refused as exc:
            # The full extractor cannot start (memory): judge the node from its import closure.
            res.line(f"full extract refused ({exc}); targeted extract of {n.module}")
            payload, index, problems = targeted_extract(ctx, n.module)
            targeted = True
    node = next((x for x in payload["nodes"] if x["name"] == n.name), None)
    if node is None:
        res.line(f"{n.name} not found after extract")
        res.exit_code = EXIT_LEAN
        return res
    res.line(f"{n.name}: sorry={node['sorry']} status={node['status']} axioms={', '.join(node['axioms']) or '-'}"
             + (f" uncovered={', '.join(node['uncovered_axioms'])}" if node["uncovered_axioms"] else ""))
    # node-to-node dependencies only: a declared core lemma is never open, so it never shows as actual
    names = {x["name"] for x in payload["nodes"]}
    declared = [d for d in node["deps_declared"] if d in names]
    actual = [d for d in node["deps_actual"] if d in names]
    if set(declared) != set(actual):
        res.line(f"  declared node dependencies differ from the proof's: declared {declared}, used {actual}")
    with Store(cfg) as st:
        outcome = "closed" if node["status"] == "closed" else ("proved-unallowlisted" if node["status"] == "unallowlisted" else "checked")
        st.event("check", n.name, ctx.agent, {"outcome": outcome, "seconds": time.time() - started})
        if node["status"] == "closed":
            st.record_attempt(n.name, ctx.agent, "closed", note="lockstep check")
    if targeted:
        res.line("targeted extract: nodes.json/status.md not updated; run `lockstep extract` when memory allows")
    res.payload = {"node": node, "build_seconds": build.seconds, "build_peak_mib": build.peak_mib,
                   "targeted": targeted}
    res.exit_code = EXIT_OK
    return res


command(CommandSpec(name="check", cli=["check"], long_running=True,
                    help="Build only the node's module, re-extract, and report sorry-free / axioms / build time.", run=run,
                    args=[Arg("name", "node name", positional=True, required=True), Arg("budget", "wall-clock budget in minutes", "int"),
                          Arg("async_", "submit as a background job", "bool"), Arg("force", "run even if another lean compiler is running", "bool"),
                          Arg("targeted", "extract only this node's import closure; do not rewrite the full index", "bool")]))
