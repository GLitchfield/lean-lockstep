"""`lockstep rules`: the agent rule files (for example CLAUDE.md and AGENTS.md) carry one rule list.

Configured in ``[rules]``: ``files`` lists the files whose rule lines (lines starting with
``- ``) must be identical, and ``require`` lists phrases some rule line must contain. No
Lean, no database: CI can run it on a bare checkout."""

from __future__ import annotations

from lockstep import EXIT_CHECK_FAILED, EXIT_OK
from lockstep.context import Ctx
from lockstep.output import Result
from lockstep.spec import CommandSpec, command


def rule_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def problems(files: dict[str, str], require: list[str]) -> list[str]:
    """Problems with rule files given as {name: text}: the first file is the reference."""
    out = []
    names = list(files)
    if not names:
        return out
    ref = rule_lines(files[names[0]])
    if not ref:
        out.append(f"{names[0]} has no rule lines (lines starting with '- ')")
    for name in names[1:]:
        if rule_lines(files[name]) != ref:
            out.append(f"the rule list in {name} differs from {names[0]}: edit both")
    for phrase in require:
        if not any(phrase in line for line in ref):
            out.append(f"no rule in {names[0]} mentions `{phrase}`")
    return out


def run(ctx: Ctx) -> Result:
    cfg = ctx.cfg
    res = Result()
    names = [str(f) for f in cfg.get("rules", "files", default=[]) or []]
    require = [str(p) for p in cfg.get("rules", "require", default=[]) or []]
    if not names:
        res.line("no rule files configured ([rules] files)")
        res.exit_code = EXIT_OK
        return res
    texts, missing = {}, []
    for name in names:
        path = cfg.root / name
        if path.is_file():
            texts[name] = path.read_text()
        else:
            missing.append(f"{name} not found")
    found = missing + problems(texts, require)
    res.human.extend(found or [f"rules OK: {', '.join(names)} carry the same {len(rule_lines(texts[names[0]]))} rules"])
    res.payload = {"files": names, "problems": found}
    res.exit_code = EXIT_CHECK_FAILED if found else EXIT_OK
    return res


command(CommandSpec(name="rules", cli=["rules"], run=run, args=[],
                    help="Check that the configured agent rule files carry the same rule list and the required rules."))
