from __future__ import annotations

from lockstep import EXIT_CHECK_FAILED, EXIT_OK
from lockstep.allowlist import read_allowlist, uncovered
from lockstep.context import Ctx
from lockstep.gitutil import changed_files
from lockstep.modules import module_to_path
from lockstep.nodes import dests_from_json, load_nodes_json, nodes_from_json
from lockstep.output import Result, table
from lockstep.spec import Arg, CommandSpec, command


def run(ctx: Ctx, changed: bool = False) -> Result:
    cfg = ctx.cfg
    data = load_nodes_json(cfg)
    nodes = nodes_from_json(data)
    dests = dests_from_json(data)
    allow = read_allowlist(cfg.allowlist_file)
    std = cfg.standard_axioms
    subjects = [(n.name, n.module, n.nonstandard_axioms, n.sorry) for n in nodes] + [(d.name, d.module, d.nonstandard_axioms, d.sorry) for d in dests]
    if changed:
        files = set(changed_files(cfg, f"{cfg.git_remote}/{cfg.default_branch}"))
        subjects = [s for s in subjects if str(module_to_path(s[1])) in files]
    rows = []
    failures = []
    for name, module, axioms, sorry in subjects:
        unc = uncovered(allow, name, [a for a in axioms if a != "sorryAx"], std)
        if unc:
            failures.append(f"{name}: uncovered axiom(s) {', '.join(unc)}")
        rows.append([name, ", ".join(a for a in axioms if a != "sorryAx") or "-", ", ".join(unc) or "", "sorry" if sorry else ""])
    res = Result()
    res.human.extend(table(rows, ["declaration", "non-standard axioms", "uncovered", ""]))
    if failures:
        res.line("")
        res.line(f"audit FAILED: {len(failures)} declaration(s) reach axioms without an allowlist entry")
        res.human.extend(f"  {f}" for f in failures)
        res.exit_code = EXIT_CHECK_FAILED
    else:
        res.line(f"audit OK: {len(rows)} declaration(s), every non-standard axiom is allowlisted")
        res.exit_code = EXIT_OK
    res.payload = {"audited": len(rows), "failures": failures, "allowlist_entries": len(allow)}
    return res


command(CommandSpec(name="audit", cli=["audit"], help="Axiom audit of blueprint nodes and destination theorems against harness/allowlist.json.", run=run,
                    args=[Arg("changed", "only declarations whose modules differ from the default branch", "bool")]))
