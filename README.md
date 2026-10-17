# lean-lockstep

A project harness for Lean 4 formalization work done by several agents at once: AI coding agents
of any kind (anything that can run a shell command or call an MCP tool) and people. It keeps the
state of the project (which statements are open, claimed, proved, and what depends on what)
derived from the compiled Lean environment, and it records who is doing what, why, and with how
much memory.

- **State comes from Lean, not from a document.** Each open statement is a *node*: a theorem
  with a `sorry` in its own blueprint module, tagged in its docstring. `lockstep extract` reads the
  compiled environment and reports each node as open, claimed or closed (sorry-free with
  allowlisted axioms). There is no hand-kept status file that can drift.
- **Approved statements are locked.** `harness/statements.lock` records the type hash of every
  approved statement. A proof that quietly weakens its statement shows up as a lock violation;
  changing a statement takes `lockstep node restate`, which logs who approved it and why. This is the
  failure mode that matters most when agents write the proofs.
- **Agents coordinate through the harness.** Claims expire and carry the intent (`--why`); every
  release records an outcome; `lockstep log` and `lockstep journal` keep the trail; long commands run as
  recorded, memory-capped jobs that every agent can see.
- **Audits scale.** The axiom audit is a per-module taint scan with a cache, which finishes on
  projects where `#print axioms` does not.
- **No client or model is assumed.** Every command has a CLI and an MCP tool. A git pre-commit
  hook guards commits for everyone; optional Claude Code hooks add a session banner and a guard
  against compute launched outside the harness.

Status: experimental (0.1). Built and used in two research projects; expect rough edges outside
them. Linux first.

## Requirements

- Python 3.11 or newer. The core has no dependencies; extras: `mcp` (MCP server), `test`
  (pytest), `research` (NumPy for research-library probes).
- Lean 4 through [elan](https://github.com/leanprover/elan), Lake, and git.
- The memory guard runs each `lake` invocation in a capped `systemd-run --user` scope, so it
  needs Linux with a systemd user session. Elsewhere set `[build] guard = false`.
- `bwrap` (bubblewrap) for the sandboxed proof checks of the experimental automation layer.

## Try it

`examples/minimal` is a Lean-only Lake project (no Mathlib) with one core lemma and an empty
blueprint. From a checkout of this repository:

```console
cd examples/minimal
export LOCKSTEP_AGENT=me:demo            # every claim, job and log entry is recorded under this id
../../bin/lockstep build core
../../bin/lockstep extract               # writes harness/nodes.json, index.json, status.md
../../bin/lockstep node add --name double_succ --ws A --kind formalization \
  --type "(n : Nat) : (n + 1) + (n + 1) = 2 * n + 2" \
  --description "Doubling a successor." --depends double --dangling-ok "example"
../../bin/lockstep ready                 # Demo.double_succ is open and ready
../../bin/lockstep claim double_succ --why "rewrite with Demo.double, then omega"
# replace the `sorry` in DemoBlueprint/A/DoubleSucc.lean with `rw [double]; omega`
../../bin/lockstep check double_succ     # sorry=False status=closed
../../bin/lockstep close double_succ     # moves it into the core library, records the attempt
../../bin/lockstep audit && ../../bin/lockstep journal
```

`tests/test_example_lifecycle.py` runs the same sequence whenever `lake` is installed. To start
over, run `git checkout -- . && git clean -fdx .` in `examples/minimal`.

## Installing

The harness writes Lean files into the project (the extractor, node modules) and its lock hashes
depend on the toolchain, so pin the version a project uses. Either:

- **From PyPI**, which puts `lockstep` and `lockstep-mcp` on the `PATH`:
  ```console
  uv tool install "lean-lockstep[mcp]==0.1.0"     # or: pipx install "lean-lockstep[mcp]==0.1.0"
  ```
  Without PyPI access, install a tag from git instead:
  `uv tool install "lean-lockstep[mcp] @ git+https://github.com/GLitchfield/lean-lockstep@v0.1.0"`.
- **As a git submodule**, pinned by commit and upgraded by bumping the pin, which suits a project
  that also develops the harness:
  ```console
  git submodule add https://github.com/GLitchfield/lean-lockstep tools/lockstep
  uv venv tools/lockstep/.venv && uv pip install -e "tools/lockstep[mcp]"
  ```
  `tools/lockstep/bin/lockstep` and `tools/lockstep/bin/lockstep-mcp` prefer that virtualenv and
  fall back to the system `python3`.

The commands below write `lockstep`; with a submodule, that is `tools/lockstep/bin/lockstep`.

## Adopting it in a project

1. Give the Lake package two libraries: a core library (proved results only) and a blueprint
   library whose root file imports every node module. `lockstep node add` maintains that root.
2. Copy `examples/minimal/harness/` and edit `harness/config.toml`: namespaces, layers, build
   targets, memory caps. Start `statements.lock` as `{}` and `allowlist.json` as `[]`.
3. Ignore the harness state directory (`.lockstep/` with `[project] state_home = ".lockstep"`).
4. Write the agent rules into the files your clients read (`AGENTS.md`, `CLAUDE.md`, ...) and
   list them under `[rules]`; `lockstep rules` checks that they carry the same rule list (cheap enough
   for CI). `examples/minimal/AGENTS.md` is a starting point. Tell each agent to export a stable
   `LOCKSTEP_AGENT` (for example `<client>:<session id>`); claims and logs are recorded under it.
5. Guard commits with the git hook, which works whoever commits:
   ```console
   printf '#!/bin/sh\nexec lockstep hook pre-commit\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
   ```
   It refuses a commit that fails `lockstep layers`, `lockstep extract --check` (generated files out of date
   or a locked statement changed) or `lockstep audit --changed`.
6. Register the MCP server with your client: command `lockstep-mcp` (or `tools/lockstep/bin/lockstep-mcp`), stdio,
   no arguments. For example `.mcp.json`:
   ```json
   { "mcpServers": { "lockstep": { "command": "tools/lockstep/bin/lockstep-mcp", "args": [] } } }
   ```
7. Optionally, for Claude Code, add the session and shell hooks to `.claude/settings.json`:
   ```json
   { "hooks": {
       "SessionStart": [{ "hooks": [{ "type": "command", "command": "tools/lockstep/bin/lockstep hook session-start" }] }],
       "PreToolUse": [{ "matcher": "Bash", "hooks": [{ "type": "command", "command": "tools/lockstep/bin/lockstep hook pre-bash" }] }] } }
   ```
   `session-start` prints the session's agent id and `lockstep status --brief`. `pre-bash` blocks a
   bare `nohup`/`systemd-run` (compute goes through `lockstep job run`) and runs the commit guard
   before `git commit`/`git push`. Any client whose hooks pass the same JSON on stdin can use them.
8. In CI, `python3 -m lockstep.lock BASE_LOCK HEAD_LOCK` (with `PYTHONPATH=tools/lockstep` for a submodule)
   prints the log files a lock change must touch: `restatements.md` when an approved statement
   changed or disappeared, `retired.md` for a refutation. New entries need none.
9. Optionally, `tools/lockstep/systemd/install.sh` (from a checkout) installs a `lockstep-nightly-<project
   id>` user timer (03:00) that runs the guarded core build, `lockstep extract --check` and `lockstep audit`.

## Commands

| Area | Commands |
|---|---|
| State | `status`, `ready`, `node show`, `search`, `extract`, `extract --check`, `doctor` |
| Statements | `node add`, `node restate`, `node retire`, `lock refresh` (after a toolchain bump) |
| Work | `claim`, `release`, `check`, `close`, `falsify` (conjecture nodes) |
| Builds and jobs | `build core` / `blueprint` / `node`, `job run`, `job wait`, `job list`, `worker` |
| Trail | `log`, `journal`, `note` (weekly summary) |
| Integrity | `audit`, `layers`, `rules`, `hook pre-commit` |
| Client hooks | `hook session-start`, `hook pre-bash` |

Every command takes `--json`, and all but the hooks are also MCP tools. Node names may be
dotted relative to the declaration namespace (`--name List.foo` declares `theorem List.foo` in
it), and `--depends` lists the project declarations a node waits on (library lemmas belong in
the description).

### Experimental

These work in the projects they came from but are less general; see
[`docs/automation.md`](docs/automation.md):

- **Automation layer**: durable `operation …` workflows run by `lockstep worker`, a results `inbox`,
  `proof capture|check|prove`, `node finish`, `probe run|replay`, `context`.
- **Research library**: `lib search|show|list` over a project catalog
  (`harness/research/catalog.json`).
- **Certificate layers**: `[layers]` rules for generated proof-module families, and
  `storage share` for their compiled artifacts.

## Things that cost hours to learn

- **Never `import` the project modules at the top of a `lake env lean --run` script that also
  calls `importModules` on them.** The script's own import maps the oleans; the second load
  cannot mmap at the same addresses and reads them into anonymous memory instead (37 GB and a
  cgroup OOM kill on a project with large certificate modules). The extractor template imports
  only `Lean`; `main` imports the project.
- **Never use `importAll := true`** for the extractor environment (30+ GB). A plain import
  exposes every theorem value in `env.header.moduleData`.
- **Never run bare `lake build` on big targets.** Lake 5 has no `-j`; two dozen parallel
  compilers exhaust a 20 GB guard. `lockstep build` builds Lake's out-of-date list one module per
  guarded invocation first.
- **`#print axioms` does not finish on large generated proof families** (16 TB read in 3.5 h);
  the extractor's module-order taint scan with a per-module cache is the audit path.
- **A module-system core root (`module` header) cannot import plain files.** When the core root
  uses it, nodes are written in module form from the start, so a proof elaborates against the
  same imports before and after `lockstep close`.
- **The elaborator moves parts of a proof into auxiliary declarations** (`foo._proof_1`), so a
  dependency scan of a theorem's value alone reports the auxiliaries instead of the lemmas they
  use. The extractor looks through a declaration's own auxiliaries.

## Development

```console
uv venv .venv && uv pip install -e ".[test,mcp,research]"
.venv/bin/python -m pytest
```

Releases follow semantic versioning (0.x: minor versions may break). Changes are listed in
[`CHANGELOG.md`](CHANGELOG.md). To release, bump `__version__` in `lockstep/__init__.py`, add the
changelog entry, and push a `vX.Y.Z` tag: `.github/workflows/release.yml` builds the sdist and
wheel and publishes them to PyPI through trusted publishing.

## License

MIT; see [`LICENSE`](LICENSE).
