from types import SimpleNamespace

from lockstep.context import Ctx


def test_targeted_check_never_requests_full_extraction(cfg, monkeypatch):
    from lockstep.commands import check, extract, node

    declaration = SimpleNamespace(name="TCore.foo_node", module="TBlueprint.A.Foo", short="foo_node")
    checked = {"name": declaration.name, "sorry": False, "status": "closed", "axioms": [],
               "uncovered_axioms": [], "deps_declared": [], "deps_actual": []}
    monkeypatch.setattr(check, "find_node", lambda *_: declaration)
    monkeypatch.setattr(check, "run_guarded", lambda *_args, **_kwargs:
                        SimpleNamespace(ok=True, seconds=0, peak_mib=0, summary=lambda: "ok"))
    seen = []

    def targeted(ctx, module):
        seen.append(module)
        return {"nodes": [checked]}, [], []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a targeted check must not prebuild or extract the full blueprint")

    monkeypatch.setattr(node, "targeted_extract", targeted)
    monkeypatch.setattr(extract, "run_extract", forbidden)
    result = check.run(Ctx(cfg), declaration.name, targeted=True)
    assert result.exit_code == 0
    assert result.payload["targeted"]
    assert seen == [declaration.module]
    assert not cfg.nodes_json.exists()


def test_targeted_add_never_requests_full_extraction(cfg, monkeypatch):
    from lockstep.commands import node

    seen = []
    monkeypatch.setattr(node, "elaborate_module", lambda *_args, **_kwargs: None)

    def targeted(ctx, module):
        seen.append(module)
        return {"nodes": [{"name": "TCore.regression", "type_hash": "hash", "type_pp": "True",
                           "status": "proposed"}]}, [], []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a targeted add must not prebuild or extract the full blueprint")

    monkeypatch.setattr(node, "targeted_extract", targeted)
    monkeypatch.setattr(node, "re_extract", forbidden)
    result = node.run_add(Ctx(cfg), "regression", "A", "formalization", ": True",
                          "Targeted registration regression.", dangling_ok="test", targeted=True)
    assert result.exit_code == 0
    assert seen == ["TBlueprint.A.Regression"]
    assert not cfg.nodes_json.exists()


def test_targeted_check_is_preserved_when_queued(cfg, monkeypatch):
    from lockstep.commands import check
    from lockstep import jobs

    monkeypatch.setattr(check, "find_node", lambda *_:
                        SimpleNamespace(name="TCore.foo_node", module="TBlueprint.A.Foo"))
    submitted = []

    def submit(_cfg, **kwargs):
        submitted.append(kwargs)
        return {"id": "test-job"}

    monkeypatch.setattr(jobs, "submit", submit)
    check.run(Ctx(cfg), "foo_node", async_=True, targeted=True)
    assert "--targeted" in submitted[0]["argv"]


def test_targeted_falsify_records_result_without_full_extraction(cfg, monkeypatch):
    import json
    from lockstep.commands import extract, falsify, node

    declaration = SimpleNamespace(name="TCore.foo_node", module="TBlueprint.A.Foo",
                                  short="foo_node", falsifier="probe.py",
                                  type_pp="True", type_hash="hash")
    cfg.falsifiers_dir.mkdir(parents=True, exist_ok=True)
    (cfg.falsifiers_dir / "probe.py").write_text("raise SystemExit(0)\n")
    monkeypatch.setattr(falsify, "find_node", lambda *_: declaration)
    monkeypatch.setattr(falsify.shutil, "which", lambda *_: None)
    seen = []

    def targeted(ctx, module):
        seen.append(module)
        return {}, [], []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a targeted falsifier must not extract the full environment")

    monkeypatch.setattr(node, "targeted_extract", targeted)
    monkeypatch.setattr(extract, "run_extract", forbidden)
    result = falsify.run(Ctx(cfg), declaration.name, targeted=True)
    assert result.exit_code == 0
    assert result.payload["targeted"]
    assert seen == [declaration.module]
    assert not cfg.nodes_json.exists()
    record = json.loads((cfg.falsify_results_dir / (declaration.name + ".json")).read_text())
    assert record["result"] == "pass"
    assert record["statement_type_hash"] == declaration.type_hash


def test_targeted_falsify_is_preserved_when_queued(cfg, monkeypatch):
    from lockstep.commands import falsify
    from lockstep import jobs

    declaration = SimpleNamespace(name="TCore.foo_node", falsifier="probe.py")
    cfg.falsifiers_dir.mkdir(parents=True, exist_ok=True)
    (cfg.falsifiers_dir / "probe.py").write_text("raise SystemExit(0)\n")
    monkeypatch.setattr(falsify, "find_node", lambda *_: declaration)
    submitted = []

    def submit(_cfg, **kwargs):
        submitted.append(kwargs)
        return {"id": "test-job"}

    monkeypatch.setattr(jobs, "submit", submit)
    falsify.run(Ctx(cfg), declaration.name, async_=True, targeted=True)
    assert "--targeted" in submitted[0]["argv"]


def test_targeted_conjecture_readiness_uses_matching_falsifier_result(cfg):
    import json
    from lockstep.nodes import find_node

    cfg.lock_file.write_text(json.dumps({"TCore.fresh": {
        "module": "TBlueprint.A.Fresh", "kind": "conjecture", "ws": "A",
        "type_hash": "current", "type_pp": "True"}}))
    assert not find_node(cfg, "fresh", nodes=[]).ready
    cfg.falsify_results_dir.mkdir(parents=True, exist_ok=True)
    result = cfg.falsify_results_dir / "TCore.fresh.json"
    result.write_text(json.dumps({"node": "TCore.fresh", "result": "pass",
                                 "statement_type_hash": "old"}))
    assert not find_node(cfg, "fresh", nodes=[]).ready
    result.write_text(json.dumps({"node": "TCore.fresh", "result": "pass",
                                 "statement_type_hash": "current"}))
    found = find_node(cfg, "fresh", nodes=[])
    assert found.status == "open" and found.ready
    assert not cfg.nodes_json.exists()
