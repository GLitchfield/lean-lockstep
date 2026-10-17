"""`lockstep rules`: agent rule files carry one rule list (replaces a per-project CLAUDE.md/AGENTS.md test)."""

from __future__ import annotations

RULES = "# Rules\n\nIntro text.\n\n- Run `lockstep claim` first.\n- Long commands go through `lockstep job run`.\n"


def _configure(project, files, require=()):
    config = project / "harness" / "config.toml"
    config.write_text(config.read_text() + f"\n[rules]\nfiles = {list(files)!r}\nrequire = {list(require)!r}\n".replace("'", '"'))


def _run(project):
    from lockstep.commands import rules
    from lockstep.context import Ctx

    return rules.run(Ctx.load(project))


def test_identical_rule_lists_pass(project):
    (project / "CLAUDE.md").write_text(RULES)
    (project / "AGENTS.md").write_text("# Agents\n\nOther intro.\n\n" + RULES.split("\n\n", 2)[2])
    _configure(project, ["CLAUDE.md", "AGENTS.md"], ["lockstep claim", "lockstep job run"])
    res = _run(project)
    assert res.exit_code == 0, res.human
    assert "2 rules" in res.human[0]


def test_drift_missing_rule_and_missing_file_fail(project):
    (project / "CLAUDE.md").write_text(RULES)
    (project / "AGENTS.md").write_text(RULES.replace("first", "before starting"))
    _configure(project, ["CLAUDE.md", "AGENTS.md", "GEMINI.md"], ["lockstep release"])
    res = _run(project)
    assert res.exit_code != 0
    text = "\n".join(res.human)
    assert "AGENTS.md differs from CLAUDE.md" in text
    assert "`lockstep release`" in text and "GEMINI.md not found" in text


def test_unconfigured_is_a_no_op(project):
    res = _run(project)
    assert res.exit_code == 0 and "no rule files configured" in res.human[0]
