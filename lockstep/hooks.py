"""Client and git hooks: ``lockstep hook session-start | pre-bash | pre-commit [--root DIR]``.

``pre-commit`` is an ordinary git hook, so the commit guard holds for every agent client and
for people.  ``session-start`` and ``pre-bash`` read the JSON a Claude Code hook receives on
stdin (``session_id``, ``cwd``, ``tool_input.command``); a client with a compatible hook
protocol can use them unchanged.  Exit code 2 blocks the action, with the reason on stderr.

These run before every shell command an agent issues, so they import only what they need and
run the checks as ``lockstep`` subprocesses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# `systemd-run` / `nohup` at the start of a command segment is compute nobody else can see,
# outside the memory budget; through `lockstep` itself (`lockstep job run ...`) it is recorded.
BARE_LAUNCH = re.compile(r"(?:^|[;&|(]\s*)(?:systemd-run|nohup)\b")
VIA_LOCKSTEP = re.compile(r"(?:bin/lockstep|\bes)\s+(?:job\s+run|extract|build|check|falsify|worker)\b")
# Only a real invocation counts: `git [global options] commit|push` at the start of a command
# segment, not the words inside a heredoc, a message, or a file being written.  `-C`, `-c`
# and the separated `--git-dir`-style options take an argument.
GIT_COMMIT = re.compile(r"(?:^|[;&|(]\s*)git(?:\s+(?:-[Cc]|--(?:git-dir|work-tree|namespace))\s+\S+|\s+-[-\w=./]+)*"
                        r"\s+(commit|push)\b")
# A commit whose message carries this marker may touch frozen certificate inputs; while the
# certificates rebuild the compiled state is in flux, so the extract check is skipped.
REBUILD_MARKER = "[certificate-rebuild]"
USAGE = "usage: lockstep hook session-start [--client NAME] | pre-bash | pre-commit  [--root DIR]"


def _read_input() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _root(explicit: str | None, data: dict) -> Path | None:
    from lockstep.config import find_root
    from lockstep.errors import ConfigError

    if explicit:
        return Path(explicit).resolve()
    # as the client reports it, else the working directory
    for cand in (os.environ.get("CLAUDE_PROJECT_DIR"), data.get("cwd"), None):
        try:
            return find_root(Path(cand) if cand else None)
        except ConfigError:
            continue
    return None


def _es(root: Path, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    package_parent = str(Path(__file__).resolve().parents[1])
    child = {**os.environ, **(env or {})}
    child["PYTHONPATH"] = package_parent + (os.pathsep + child["PYTHONPATH"] if child.get("PYTHONPATH") else "")
    return subprocess.run([sys.executable, "-m", "lockstep", "--root", str(root), *args], cwd=root, env=child,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def guard(root: Path, *, rebuild: bool = False) -> int:
    """The commit guard: import layering, the statement lock and the axiom audit of changed files."""
    checks = [["layers", "--base", "HEAD", *(["--allow-rebuild"] if rebuild else [])]]
    if not rebuild:
        checks.append(["extract", "--check"])
    checks.append(["audit", "--changed"])
    failed = False
    for args in checks:
        proc = _es(root, args)
        if proc.returncode != 0:
            failed = True
            tail = "\n".join(proc.stdout.splitlines()[-20:])
            print(f"lockstep {' '.join(args)} failed:\n{tail}", file=sys.stderr)
    if rebuild:
        print(f"lockstep: {REBUILD_MARKER} commit; extract consistency check skipped until the certificates are rebuilt",
              file=sys.stderr)
    if failed:
        print("blocked: fix the problems above (or run `lockstep extract` to refresh generated files) before committing.",
              file=sys.stderr)
        return 2
    return 0


def session_start(data: dict, root: Path | None, client: str) -> int:
    sid = data.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or str(os.getppid())
    agent = f"{client}:{sid}"
    print(f"lockstep: agent id LOCKSTEP_AGENT={agent} (export it in shell calls; `lockstep claim` records it).")
    print("Project state comes from `lockstep status`; pick work with `lockstep ready`.")
    print()
    if root is None:
        print("lockstep: no harness/config.toml found from this directory (set LOCKSTEP_PROJECT_ROOT or pass --root)")
        return 0
    proc = _es(root, ["status", "--brief"], env={"LOCKSTEP_AGENT": agent})
    lines = proc.stdout.splitlines()[:40]
    print("\n".join(lines) if proc.returncode == 0 or lines else "lockstep status failed (run `lockstep doctor`)")
    return 0


def pre_bash(data: dict, root: Path | None) -> int:
    command = (data.get("tool_input") or {}).get("command") or ""
    if BARE_LAUNCH.search(command) and not VIA_LOCKSTEP.search(command):
        print("blocked: launch compute through the harness so other agents can see it and the memory budget holds:\n"
              '  lockstep job run --why "<purpose>" --out <runs/... or data/...> [--node <node>] [--mem 2G] -- <command>',
              file=sys.stderr)
        return 2
    if not GIT_COMMIT.search(command) or root is None:
        return 0
    return guard(root, rebuild=REBUILD_MARKER in command)


def main(argv: list[str]) -> int:
    args = list(argv)
    options: dict[str, str] = {}
    for flag in ("--root", "--client"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(USAGE, file=sys.stderr)
                return 2
            options[flag] = args[i + 1]
            del args[i:i + 2]
    if len(args) != 1 or args[0] not in ("session-start", "pre-bash", "pre-commit"):
        print(USAGE, file=sys.stderr)
        return 2
    which = args[0]
    data = {} if which == "pre-commit" else _read_input()
    root = _root(options.get("--root"), data)
    if which == "session-start":
        return session_start(data, root, options.get("--client", "claude-code"))
    if which == "pre-bash":
        return pre_bash(data, root)
    if root is None:
        print("lockstep hook pre-commit: no harness/config.toml found (set LOCKSTEP_PROJECT_ROOT or pass --root)", file=sys.stderr)
        return 1
    return 1 if guard(root) else 0
