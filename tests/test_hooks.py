"""`lockstep hook`: the compute-launch guard, the commit guard and the session banner."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from lockstep import hooks


class Calls(list):
    """The `lockstep` subcommands a hook ran; those named in `failing` exit 1."""

    failing: tuple[str, ...] = ()

    def __call__(self, root, args, env=None):
        self.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] in self.failing else 0, stdout=f"{args[0]} output\n")


@pytest.fixture
def calls(monkeypatch):
    fake = Calls()
    monkeypatch.setattr(hooks, "_es", fake)
    return fake


def bash(command):
    return {"tool_input": {"command": command}}


@pytest.mark.parametrize("command", ["nohup python3 sweep.py &", "cd x && systemd-run --user sleep 9"])
def test_bare_launch_is_blocked(command, calls, tmp_path, capsys):
    assert hooks.pre_bash(bash(command), tmp_path) == 2
    assert "lockstep job run" in capsys.readouterr().err and not calls


def test_launch_through_es_and_git_words_in_text_pass(calls, tmp_path):
    assert hooks.pre_bash(bash('lockstep job run --why w --out runs/x -- nohup true'), tmp_path) == 0
    assert hooks.pre_bash(bash("cat > notes.md <<'EOF'\ngit commit and push\nEOF"), tmp_path) == 0
    assert hooks.pre_bash(bash('echo "then git commit"'), tmp_path) == 0
    assert not calls


def test_commit_runs_the_guard_and_blocks_on_failure(calls, tmp_path, capsys):
    assert hooks.pre_bash(bash("git -C . commit -m 'x'"), tmp_path) == 0
    assert calls == [["layers", "--base", "HEAD"], ["extract", "--check"], ["audit", "--changed"]]
    for command in ("git -c user.name=x commit", "cd sub; git --git-dir .git push", "git --no-pager commit"):
        calls.clear()
        assert hooks.pre_bash(bash(command), tmp_path) == 0 and len(calls) == 3, command
    calls.clear()
    calls.failing = ("audit",)
    assert hooks.pre_bash(bash("git push"), tmp_path) == 2
    err = capsys.readouterr().err
    assert "lockstep audit --changed failed:" in err and "audit output" in err and "blocked:" in err


def test_rebuild_marker_skips_the_extract_check(calls, tmp_path):
    assert hooks.pre_bash(bash("git commit -m 'regenerate [certificate-rebuild]'"), tmp_path) == 0
    assert calls == [["layers", "--base", "HEAD", "--allow-rebuild"], ["audit", "--changed"]]


def test_git_pre_commit_hook_uses_the_same_guard(calls, project):
    assert hooks.main(["pre-commit", "--root", str(project)]) == 0
    assert [c[0] for c in calls] == ["layers", "extract", "audit"]
    calls.failing = ("extract",)
    assert hooks.main(["pre-commit", "--root", str(project)]) == 1


def test_session_start_names_the_agent(calls, project, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "abc", "cwd": str(project)})))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("LOCKSTEP_PROJECT_ROOT", raising=False)
    assert hooks.main(["session-start", "--client", "my-client"]) == 0
    out = capsys.readouterr().out
    assert "LOCKSTEP_AGENT=my-client:abc" in out and "status output" in out
    assert calls == [["status", "--brief"]]


def test_unknown_hook_is_a_usage_error(capsys):
    assert hooks.main(["post-bash"]) == 2
    assert "usage:" in capsys.readouterr().err
