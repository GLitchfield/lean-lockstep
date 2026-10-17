from __future__ import annotations

from lockstep import EXIT_CHECK_FAILED, EXIT_OK
from lockstep.context import Ctx
from lockstep.layers import check_layers, derive_allowlists
from lockstep.modules import scan_modules, write_name_list
from lockstep.output import Result
from lockstep.spec import Arg, CommandSpec, command


def run(ctx: Ctx, write_allowlists: bool = False, allow_rebuild: bool = False, base: str | None = None) -> Result:
    cfg = ctx.cfg
    mods = scan_modules(cfg)
    res = Result()
    if write_allowlists:
        derived = derive_allowlists(cfg, mods)
        for file, names in derived.items():
            write_name_list(cfg.root / file, names, header=f"maintained by `lockstep layers --write-allowlists`; one module per line")
            res.line(f"wrote {file}: {len(names)} module(s)")
    violations = check_layers(cfg, mods, allow_rebuild=True if allow_rebuild else None, base=base)
    counts: dict[str, int] = {}
    for m in mods.values():
        counts[m.layer or "?"] = counts.get(m.layer or "?", 0) + 1
    res.payload = {
        "modules": counts,
        "violations": [{"kind": v.kind, "module": v.module, "detail": v.detail} for v in violations],
    }
    res.line("modules per layer: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if violations:
        res.exit_code = EXIT_CHECK_FAILED
        res.line(f"{len(violations)} layer violation(s):")
        for v in violations[:200]:
            res.line(f"  {v}")
        if len(violations) > 200:
            res.line(f"  ... {len(violations) - 200} more")
    else:
        res.exit_code = EXIT_OK
        res.line("layers OK")
    return res


command(
    CommandSpec(
        name="layers",
        cli=["layers"],
        help="Check import layering, the certificate allowlists, and that the blueprint root imports every node module.",
        run=run,
        args=[Arg("write_allowlists", "rewrite the allowlist / pinned-layer files from the current import graph before checking", "bool"),
              Arg("allow_rebuild", "accept changes to frozen encoding modules (a certificate rebuild is being committed)", "bool"),
              Arg("base", "git ref the frozen-module check diffs against (default origin/<default branch>; the pre-commit hook uses HEAD)")],
    )
)
