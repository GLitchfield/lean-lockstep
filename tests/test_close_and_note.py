import time

from lockstep.commands.close import rewrite_imports
from lockstep.commands.note import run as note_run
from lockstep.context import Ctx
from lockstep.store import Store


def test_rewrite_imports(cfg):
    (cfg.root / "TCore" / "User.lean").write_text("module\npublic import TBlueprint.A.Foo\nimport TCore.Basic\n")
    (cfg.root / "TCore" / "Other.lean").write_text("import TBlueprint.A.FooBar\n")
    touched = rewrite_imports(cfg, "TBlueprint.A.Foo", "TCore.Closed.A.Foo", ["TCore", "TBlueprint.lean"])
    assert "TCore/User.lean" in touched and "TCore/Other.lean" not in touched
    assert "public import TCore.Closed.A.Foo" in (cfg.root / "TCore" / "User.lean").read_text()
    assert "import TBlueprint.A.FooBar" in (cfg.root / "TCore" / "Other.lean").read_text()
    assert "import TCore.Closed.A.Foo" in (cfg.root / "TBlueprint.lean").read_text()


def test_note_from_fixture_tables(cfg):
    with Store(cfg) as st:
        st.event("node-added", "TCore.a", "t")
        st.event("closed", "TCore.b", "t")
        st.claim("TCore.c", "t", ttl_hours=1)
        st.release("TCore.c", "t", outcome="abandoned")
    ctx = Ctx(cfg=cfg, json_mode=False, agent="t")
    res = note_run(ctx, since=time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400)), write=True)
    text = "\n".join(res.human)
    assert "opened: 1" in text and "closed: 1" in text and "attempts started this week: 1" in text and "## Judgment" in text
    assert (cfg.root / res.payload["path"]).exists()


def test_close_converts_moved_file_to_module_form():
    from lockstep.commands.close import to_module_form
    src = "import Demo.Basic\n\nnamespace Demo\n\n/-- doc -/\ntheorem t : True := trivial\n\nend Demo\n"
    out = to_module_form(src)
    assert out.startswith("module\n\npublic import Demo.Basic\n\n@[expose] public section\n\n")
    assert out.endswith("end Demo\n\nend\n")
    assert to_module_form(out) is None


def test_close_inlines_an_import_of_the_core_root():
    """A node importing the core root would form a cycle once the root imports the moved
    module, so `lockstep close` replaces that import by the root's own imports."""
    from lockstep.commands.close import inline_root_import
    node = "module\n\npublic import Demo\n\n@[expose] public section\n\ntheorem t : True := trivial\n\nend\n"
    root = "module\n\npublic import Demo.Basic\npublic import Demo.Upstream.Foo\n"
    out = inline_root_import(node, "Demo", root)
    assert out.startswith("module\n\npublic import Demo.Basic\npublic import Demo.Upstream.Foo\n\n@[expose]")
    assert inline_root_import(out, "Demo", root) is None
    assert inline_root_import("import Demo.Basic\n", "Demo", root) is None
