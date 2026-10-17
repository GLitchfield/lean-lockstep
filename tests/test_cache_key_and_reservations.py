"""Extract cache key, background build reservations, and statement-lock diffs for CI."""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import ROOT


def _commit(root, msg):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg], cwd=root, check=True)


def test_dependency_bump_invalidates_the_extract_cache(project):
    """A toolchain or manifest bump changes the environment of every module without touching
    a project source, so it must change the cache key."""
    from lockstep.commands.extract import state_dirs
    from lockstep.config import load_config
    from lockstep.gitutil import tree_hash

    (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0-rc2\n")
    (project / "lake-manifest.json").write_text('{"packages": [{"rev": "old"}]}\n')
    _commit(project, "pins")
    cfg = load_config(project)
    before = tree_hash(cfg, state_dirs(cfg))
    (project / "lake-manifest.json").write_text('{"packages": [{"rev": "new"}]}\n')
    assert tree_hash(cfg, state_dirs(cfg)) != before
    _commit(project, "bump")
    bumped = tree_hash(cfg, state_dirs(cfg))
    assert bumped != before
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    assert tree_hash(cfg, state_dirs(cfg)) != bumped


def test_configured_state_dirs_extend_the_extractor_inputs(project):
    from lockstep.commands.extract import state_dirs
    from lockstep.config import load_config

    config = project / "harness" / "config.toml"
    config.write_text(config.read_text() + '\n[extract]\nstate_dirs = ["TBlueprint"]\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    dirs = state_dirs(load_config(project))
    for path in ("TBlueprint", "TBlueprint.lean", "harness/destinations.txt", "TCore", "TCore.lean", "lean-toolchain"):
        assert path in dirs


def test_background_build_reserves_its_largest_stage(project):
    """`lockstep build --async` runs a final parallel stage at memory_max_final and may retry a single
    module at memory_max_retry; a job's stages may not exceed its reservation."""
    from lockstep.build import largest_build_memory
    from lockstep.config import load_config
    from lockstep.jobs import job_memory_gib, submit
    from lockstep.store import Store

    config = project / "harness" / "config.toml"
    config.write_text(config.read_text().replace(
        "[build]\nguard = false\n",
        '[build]\nguard = false\nmemory_max = "12G"\nmemory_max_retry = "20G"\nmemory_max_final = "16G"\n'))
    cfg = load_config(project)
    assert largest_build_memory(cfg) == "20G"
    job = submit(cfg, kind="build", node=None, argv=["build", "core"], agent="tester", queue=True)
    with Store(cfg) as st:
        assert st.job(job["id"])["memory_max"] == "20G"
    assert job_memory_gib(cfg, {"kind": "check"}) == 20.0


def test_statement_changes_name_the_log_each_change_needs():
    from lockstep.lock import statement_changes

    old = {"N.a": {"type_hash": "1"}, "N.b": {"type_hash": "2"}, "N.c": {"type_hash": "3"},
           "N.d": {"type_hash": "4"}}
    new = {"N.a": {"type_hash": "1", "merged_module": "N.Upstream.A"},   # lockstep close metadata
           "N.b": {"type_hash": "9"},                                    # lockstep node restate
           "N.c": {"type_hash": "8", "kind": "refutation"},              # retired as refuted
           "N.e": {"type_hash": "5"}}                                    # lockstep node add; N.d removed
    assert statement_changes(old, new) == {"N.b": "restatements.md", "N.c": "retired.md", "N.d": "restatements.md"}
    assert statement_changes({}, {"N.e": {"type_hash": "5"}}) == {}


def test_lock_diff_cli_for_ci(tmp_path):
    base, head = tmp_path / "base.lock", tmp_path / "head.lock"
    base.write_text(json.dumps({"N.a": {"type_hash": "1"}}))
    head.write_text(json.dumps({"N.a": {"type_hash": "1", "merged_module": "M"}, "N.b": {"type_hash": "2"}}))
    run = lambda: subprocess.run([sys.executable, "-m", "lockstep.lock", str(base), str(head)], cwd=ROOT,
                                 capture_output=True, text=True, check=True).stdout.strip()
    assert run() == ""
    head.write_text(json.dumps({"N.a": {"type_hash": "7"}}))
    assert run() == "restatements.md"


def test_cache_key_follows_content_in_a_repository_subdirectory(tmp_path):
    """A project below the repository root (a monorepo, or the bundled example): edits to
    tracked and untracked files under its state dirs must change the key."""
    from types import SimpleNamespace

    from lockstep.gitutil import tree_hash

    repo, root = tmp_path / "repo", tmp_path / "repo" / "sub"
    (root / "BP").mkdir(parents=True)
    (root / "BP" / "Node.lean").write_text("theorem t : True := by sorry\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    _commit(repo, "init")
    cfg = SimpleNamespace(root=root, git_dir=None)
    committed = tree_hash(cfg, ["BP"])
    (root / "BP" / "Node.lean").write_text("theorem t : True := trivial\n")
    edited = tree_hash(cfg, ["BP"])
    assert edited != committed
    (root / "BP" / "New.lean").write_text("theorem u : True := by sorry\n")
    untracked = tree_hash(cfg, ["BP"])
    (root / "BP" / "New.lean").write_text("theorem u : True := trivial\n")
    assert tree_hash(cfg, ["BP"]) not in (committed, edited, untracked)


def test_extractor_change_invalidates_the_extract_cache(project, monkeypatch):
    """The extractor script is regenerated only when it runs, so the cache key must cover the
    extractor itself: after a harness upgrade the old extractor's output may not be reused."""
    from types import SimpleNamespace

    from lockstep.commands import extract
    from lockstep.context import Ctx

    runs = []

    def fake(cfg, **kwargs):
        runs.append(1)
        raw = {"declarations": [], "destinations": [], "modules": 0, "toolchain": "t", "nonstandard_axioms": [],
               "taint": {}}
        return SimpleNamespace(nodes=raw, index=[], result=SimpleNamespace(seconds=0, peak_mib=0))

    monkeypatch.setattr(extract, "run_extractor", fake)
    template = project / "harness" / "Extract.lean.tmpl"
    template.write_text("-- extractor v1\n")
    config = project / "harness" / "config.toml"
    config.write_text(config.read_text() + '\n[extract]\ntemplate = "harness/Extract.lean.tmpl"\n')
    extract.run_extract(Ctx.load(project), write=False)
    extract.run_extract(Ctx.load(project), write=False)
    assert len(runs) == 1
    template.write_text("-- extractor v2\n")
    payload, _, _ = extract.run_extract(Ctx.load(project), write=False)
    assert len(runs) == 2
    assert "@" not in payload["tree_hash"]  # the published tree hash is unchanged in form
