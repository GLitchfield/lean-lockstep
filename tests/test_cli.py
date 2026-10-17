import json
import subprocess
import sys

from lockstep.cli import main


def run(args, capsys):
    rc = main(args)
    out = capsys.readouterr().out
    return rc, out


def test_layers_cli_json(cfg, capsys):
    rc, out = run(["layers", "--json"], capsys)
    assert rc == 0
    data = json.loads(out)
    assert data["ok"] and data["violations"] == []


def test_doctor_runs(cfg, capsys):
    rc, out = run(["doctor"], capsys)
    assert "config" in out


def test_status_needs_extract(cfg, capsys):
    rc, out = run(["status"], capsys)
    assert rc == 2


def test_mcp_tool_signatures():
    from lockstep.mcp_server import _make_tool
    from lockstep.spec import REGISTRY
    import lockstep.commands  # noqa: F401
    import inspect

    spec = next(s for s in REGISTRY if s.name == "node_add")
    tool = _make_tool(spec)
    params = inspect.signature(tool).parameters
    assert "name" in params and params["name"].default is inspect.Parameter.empty
    assert "force" in params and params["force"].default is False


def test_flag_names_strip_trailing_underscore():
    from lockstep.cli import build_parser

    parser = build_parser()
    text = parser.format_help()
    sub = None
    for action in parser._subparsers._group_actions:
        sub = action
    checks = sub.choices["check"]
    assert "--async" in checks.format_help() and "--async-" not in checks.format_help()
    node = sub.choices["node"]
    add = [a for a in node._subparsers._group_actions][0].choices["add"]
    assert "--import IMPORT" in add.format_help()


def test_dispatch_drops_unset_optional_args(cfg):
    from lockstep.cli import build_parser, dispatch
    from lockstep.spec import CommandSpec, Arg
    from lockstep.output import Result

    seen = {}

    def handler(ctx, name, generators="default,globs"):
        seen.update(name=name, generators=generators)
        return Result()

    spec = CommandSpec(name="t", cli=["t"], help="t", run=handler, args=[Arg("name", "n", positional=True, required=True), Arg("generators", "g")])
    parser = build_parser()
    ns = parser.parse_args(["layers"])
    ns.name = "x"; ns.generators = None
    dispatch(spec, ns)
    assert seen == {"name": "x", "generators": "default,globs"}


def test_lock_refresh_only_when_statement_text_unchanged(cfg, capsys, monkeypatch):
    import json
    from lockstep.cli import main
    nodes = {"generated_at": "x", "git_sha": "y", "nodes": [
        {"name": "TCore.a", "module": "TBlueprint.A.A", "type_pp": "True", "type_hash": "new", "sorry": True, "axioms": [], "tags": {"ws": "A", "kind": "formalization"}},
        {"name": "TCore.b", "module": "TBlueprint.A.B", "type_pp": "False", "type_hash": "new2", "sorry": True, "axioms": [], "tags": {"ws": "A", "kind": "formalization"}}],
        "destinations": []}
    (cfg.root / "harness" / "nodes.json").write_text(json.dumps(nodes))
    (cfg.root / "harness" / "statements.lock").write_text(json.dumps({
        "TCore.a": {"type_hash": "old", "type_pp": "True"}, "TCore.b": {"type_hash": "old2", "type_pp": "True ∧ True"}}))
    rc = main(["lock", "refresh", "--reason", "toolchain bump", "--all", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["refreshed"] == ["TCore.a"]
    lock = json.loads((cfg.root / "harness" / "statements.lock").read_text())
    assert lock["TCore.a"]["type_hash"] == "new" and lock["TCore.b"]["type_hash"] == "old2"
