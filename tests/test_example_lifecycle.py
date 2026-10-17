"""End to end on examples/minimal with a real Lean toolchain: extract, add a node that depends on
a core lemma, claim, prove, check, close, audit. Skipped when `lake` is not installed."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT

EXAMPLE = ROOT / "examples" / "minimal"

pytestmark = pytest.mark.skipif(shutil.which("lake") is None, reason="needs a Lean toolchain (elan)")


def test_example_node_lifecycle(tmp_path):
    proj = tmp_path / "minimal"
    shutil.copytree(EXAMPLE, proj, ignore=shutil.ignore_patterns(".lockstep", ".lake"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOCKSTEP_")}
    env.update(LOCKSTEP_AGENT="tester", PYTHONPATH=str(ROOT))

    def lockstep(*args, ok=True):
        p = subprocess.run([sys.executable, "-m", "lockstep", *args], cwd=proj, env=env, capture_output=True, text=True,
                           timeout=900)
        if ok:
            assert p.returncode == 0, f"lockstep {' '.join(args)}:\n{p.stdout}\n{p.stderr}"
        return p.stdout

    def git(*args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=proj, check=True,
                       capture_output=True)

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "example")
    lockstep("build", "core")
    lockstep("extract")
    lockstep("node", "add", "--name", "double_succ", "--ws", "A", "--kind", "formalization",
       "--type", "(n : Nat) : (n + 1) + (n + 1) = 2 * n + 2", "--description", "Doubling a successor.",
       "--depends", "double", "--dangling-ok", "example node")
    assert "Demo.double_succ" in lockstep("ready")
    lockstep("claim", "double_succ", "--why", "rewrite with Demo.double")
    assert "sorry=True" in lockstep("check", "double_succ", ok=False)
    node = proj / "DemoBlueprint" / "A" / "DoubleSucc.lean"
    node.write_text(node.read_text().replace("  sorry\n", "  rw [double]; omega\n"))
    checked = lockstep("check", "double_succ")
    assert "sorry=False status=closed" in checked and "differ" not in checked
    # a proof that uses another node records it; the node's own auxiliary `_proof_N`
    # declarations (here from omega) are looked through, not listed
    lockstep("node", "add", "--name", "double_succ_succ", "--ws", "A", "--kind", "formalization",
       "--type", "(n : Nat) : (n + 2) + (n + 2) = 2 * n + 4", "--description", "Twice more.",
       "--depends", "double_succ", "--dangling-ok", "example node")
    node2 = proj / "DemoBlueprint" / "A" / "DoubleSuccSucc.lean"
    node2.write_text(node2.read_text().replace("import Demo\n", "import Demo\nimport DemoBlueprint.A.DoubleSucc\n")
                     .replace("  sorry\n", "  have := double_succ (n + 1); omega\n"))
    checked = lockstep("check", "double_succ_succ")
    assert "sorry=False status=closed" in checked and "differ" not in checked, checked
    import json
    nodes = {x["name"]: x for x in json.loads((proj / "harness" / "nodes.json").read_text())["nodes"]}
    assert nodes["Demo.double_succ_succ"]["deps_actual"] == ["Demo.double_succ"]
    assert nodes["Demo.double_succ"]["deps_actual"] == []
    lockstep("close", "double_succ")
    assert "import Demo.Closed.A.DoubleSucc" in (proj / "Demo.lean").read_text()
    assert "import Demo.Basic" in (proj / "Demo" / "Closed" / "A" / "DoubleSucc.lean").read_text()
    assert "audit OK" in lockstep("audit")
    lockstep("extract", "--check")
    lockstep("rules")
