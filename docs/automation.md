# Automation layer (experimental)

The automation layer runs multi-step work (proof checks, tactic attempts, finite searches, the
finish of a node) as durable *operations* that `lockstep worker` executes through the same guarded,
budgeted jobs as `lockstep job run`. It calls no model: whichever agent submits an operation reads
the result later, so it works the same for every client. Nothing in this layer establishes a
theorem: only the compiled Lean environment, `lockstep extract` and the audit do.

Every command here is also an MCP tool. Results are compact by default; large fields are stored
as immutable SHA-256 artifacts and referenced by id (`lockstep artifact show ID --offset N`, or repeat
the command with `--full`).

## Operations

```console
lockstep operation submit --spec work.json [--key IDEMPOTENCY_KEY]
lockstep operation show OP_ID          # stage, stages_done, stages_total, receipt references
lockstep operation list
lockstep operation resume|retry|cancel OP_ID
lockstep operation reconcile           # detect dead runners without replaying uncertain effects
```

A spec is a finite list of uniquely named stages, each with a memory cap and a time limit
(defaults: `[jobs] default_memory_max`, 10 minutes):

```json
{"schema_version": 1, "stages": [
  {"name": "sweep", "action": "command", "argv": ["python3", "scripts/sweep.py"], "memory": "2G", "minutes": 30}
]}
```

- Stages run in order; a stage that succeeded is never rerun by `resume` or `retry`.
- Submission is idempotent per agent: the same spec (or the same `--key`) returns the existing
  operation.
- Admission is atomic against the shared memory budget (`[jobs]`), so concurrent workers never
  overcommit. A request that can never fit is reported as `blocked_resource`.
- Cancellation is a request; the operation counts as cancelled once the owning worker confirms
  the process is gone. A runner that died mid-stage becomes `unknown_execution` and is never
  replayed automatically.
- `lockstep resources reconcile` releases a memory reservation only after confirming its cgroup stopped.

Restart `lockstep worker` after upgrading the harness, so that it runs the current code.

## Results inbox

A finished operation leaves an event in its submitter's inbox (keyed by `LOCKSTEP_AGENT`):

```console
lockstep inbox list
lockstep inbox claim --consumer ID     # lease one event (at-least-once delivery)
lockstep inbox ack EVENT_ID --consumer ID
lockstep inbox pause|resume            # stop or restart delivery; compute continues
```

The inbox wakes nobody. A client that wants push delivery polls `inbox claim`, or keeps its turn
open with `lockstep job wait`.

## Proofs

```console
lockstep proof capture NODE [--start BYTE --end BYTE]    # freeze the proof span and its source/dependency identities
lockstep proof check FIXTURE_ID --body body.lean [--memory 4G --minutes 10]
lockstep proof prove FIXTURE_ID [--tactics omega simp ...]
lockstep node finish NODE --body body.lean [--extract-memory 16G]
```

- `proof capture` records the exact UTF-8 byte span of a node's proof plus hashes of the source
  file, its compiled dependencies and the toolchain. Any later change to them requires a recapture.
- `proof check` compiles the candidate in a network-less `bwrap` sandbox that sees only the
  compiled libraries and the toolchain, then audits the whole parent declaration in a separate
  Lean process. Without `bwrap` it refuses rather than falling back. Failed checks return bounded
  error headers with byte offsets into the full diagnostic artifact.
- `proof prove` tries a fixed portfolio of tactics (`rfl`, `simp`, `simp_all`, `omega`,
  `norm_num`, `ring`, `linarith`, `nlinarith`, `positivity`, `grind`, `aesop`) and stops at the
  first one the kernel accepts.
- `node finish` checks the claim, branch and statement lock, checks the candidate, substitutes
  only its proof span, rebuilds the original module, runs a full extraction and the audits, and
  writes a reviewable diff. It never commits or publishes (`--policy review` is the only policy).

## Finite probes

A probe is a finite computation (a search for a counterexample, a bounded enumeration) run by a
project plugin registered in `harness/config.toml`:

```toml
[research.plugins.my_search]
module = "harness.research.my_search"        # a module in the project
sources = ["harness/research/my_search.py"]  # hashed at admission; a change refuses the run
semantics = ["my-search-v1"]                 # the spec must name one of these
```

The module provides `evaluate(spec) -> dict` (with an optional `witness`) and
`replay(witness) -> {"replayed": bool, ...}`. A spec names the plugin, its semantics and a
positive case and time budget:

```console
lockstep probe run --spec probe.json
lockstep probe replay RESULT_ARTIFACT_ID      # recompute a saved witness independently
```

A witness is replayed before the result is stored. A probe result never closes or refutes a
node: a counterexample still needs a Lean refutation, and a bounded negative proves nothing.

## Context and the research library

- `lockstep context NODE [--previous BUNDLE_ID]` returns a content-addressed bundle with the node, its
  source hash, the signatures of the declarations it uses, recent events and operations, and
  whether the index is fresh. With `--previous` it returns only what changed.
- `lockstep lib search "TASK"`, `lockstep lib show ID` and `lockstep lib list` search the project's curated catalog
  of reusable code, `harness/research/catalog.json`: `schema_version: 1` and a list of entries
  with `id`, `kind` (`primitive` or `workflow`), `title`, `summary`, `api`, `example`,
  `semantics`, `keywords` and an optional `source`. The symbols a source lists are checked by
  parsing it, never by importing it. A miss is a reason to search the source, not evidence that
  nothing exists.

## Certificate layers and the artifact cache

For projects with large generated proof modules (SAT certificates, for example):

- A `[layers]` entry can mark a layer as generated, pin its member list, and freeze the
  hand-written import closure it depends on; `lockstep layers` enforces this. `lockstep layers
  --allow-rebuild` accepts changes to those inputs, and the Claude Code hook passes it for a
  commit whose message carries `[certificate-rebuild]`.
- `[build.artifact_cache]` and `lockstep storage share` verify byte-identical compiled artifacts and
  hard-link them through Lake's immutable cache, which saves disk across worktrees.

## Index shadowing

`lockstep index shadow` merges an incremental update of the index and compares it with a supplied full
extraction. It checks incremental extraction and never publishes an index.
