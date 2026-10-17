"""`lockstep node add`: dotted names, declared dependencies, and module-form node files."""

from __future__ import annotations

import pytest


def _fake_lean(monkeypatch, node_cmd, full: str, module: str):
    payload = ({"nodes": [{"name": full, "type_hash": "h", "type_pp": "True", "status": "open", "module": module}],
                "destinations": [], "duplicate_problems": [], "problems": []}, [], [])
    monkeypatch.setattr(node_cmd, "elaborate_module", lambda ctx, module, budget=20: None)
    monkeypatch.setattr(node_cmd, "re_extract", lambda ctx: payload)


def test_local_name_is_relative_to_the_declaration_namespace(cfg):
    from lockstep.nodes import local_name

    assert local_name(cfg, "foo") == "foo"
    assert local_name(cfg, "List.foo") == "List.foo"
    assert local_name(cfg, "TCore.List.foo") == "List.foo"


def test_dotted_name_is_declared_with_its_namespace(project, monkeypatch):
    """`--name List.bar_node` declares `theorem List.bar_node` inside the declaration
    namespace, so the extractor reports `TCore.List.bar_node`, the name the lock gets."""
    from lockstep import proofs
    from lockstep.commands import node as node_cmd
    from lockstep.context import Ctx
    from lockstep.lock import read_lock

    ctx = Ctx.load(project)
    _fake_lean(monkeypatch, node_cmd, "TCore.List.bar_node", "TBlueprint.A.BarNode")
    node_cmd.run_add(ctx, name="List.bar_node", ws="A", kind="formalization", type=": True",
                     description="bar", dangling_ok="test")
    text = (project / "TBlueprint" / "A" / "BarNode.lean").read_text()
    assert "theorem List.bar_node : True := by" in text
    assert "TCore.List.bar_node" in read_lock(ctx.cfg.lock_file)
    # the proof splicer finds the same declaration
    start, end = proofs.whole_span(text, "List.bar_node")
    assert text.encode()[start:end].decode().split() == ["by", "sorry"]


def test_depends_resolve_to_project_declarations(cfg):
    from lockstep.errors import UsageError
    from lockstep.nodes import resolve_depends

    assert resolve_depends(cfg, ["dest", "TCore.dest"]) == ["TCore.dest", "TCore.dest"]
    with pytest.raises(UsageError, match="IGame.birthday_dyadic"):
        resolve_depends(cfg, ["dest", "IGame.birthday_dyadic"])


def test_unresolved_depends_are_refused_before_writing(project, monkeypatch):
    """A declared dependency outside the project would block the node forever (readiness waits
    on it), so `lockstep node add` refuses it instead of writing the module."""
    from lockstep.commands import node as node_cmd
    from lockstep.context import Ctx
    from lockstep.errors import UsageError

    ctx = Ctx.load(project)
    _fake_lean(monkeypatch, node_cmd, "TCore.bar_node", "TBlueprint.A.BarNode")
    with pytest.raises(UsageError, match="Mathlib.foo"):
        node_cmd.run_add(ctx, name="bar_node", ws="A", kind="formalization", type=": True",
                         description="bar", depends=["Mathlib.foo"], dangling_ok="test")
    assert not (project / "TBlueprint" / "A" / "BarNode.lean").exists()


def test_node_is_module_form_when_the_core_root_is(project, monkeypatch):
    """With a module-system core root the node is written as `lockstep close` would leave it, so the
    imports it elaborates against do not change when it moves into the core library."""
    from lockstep.commands import node as node_cmd
    from lockstep.commands.close import to_module_form
    from lockstep.context import Ctx

    (project / "TCore.lean").write_text("module\n\npublic import TCore.Basic\n")
    ctx = Ctx.load(project)
    _fake_lean(monkeypatch, node_cmd, "TCore.bar_node", "TBlueprint.A.BarNode")
    node_cmd.run_add(ctx, name="bar_node", ws="A", kind="formalization", type=": True",
                     description="bar", dangling_ok="test")
    text = (project / "TBlueprint" / "A" / "BarNode.lean").read_text()
    assert text.startswith("module\n\npublic import TCore\n\n@[expose] public section\n")
    assert to_module_form(text) is None


def test_node_stays_plain_when_the_core_root_is_plain(project, monkeypatch):
    from lockstep.commands import node as node_cmd
    from lockstep.context import Ctx

    ctx = Ctx.load(project)
    _fake_lean(monkeypatch, node_cmd, "TCore.bar_node", "TBlueprint.A.BarNode")
    node_cmd.run_add(ctx, name="bar_node", ws="A", kind="formalization", type=": True",
                     description="bar", dangling_ok="test")
    assert (project / "TBlueprint" / "A" / "BarNode.lean").read_text().startswith("import TCore\n")
