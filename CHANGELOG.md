# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org);
before 1.0, a minor version may break compatibility.

## 0.1.0 (unreleased)

First public release. Extracted from the harness of two Lean research projects and generalized.

- State derived from the compiled environment: `lockstep extract` reports every blueprint node as
  open, claimed or closed; `status`, `ready`, `node show`, `search`.
- Locked statements: `harness/statements.lock`, `node add`, `node restate`, `node retire`,
  `lock refresh`, and `python -m lockstep.lock` for CI.
- Coordination: claims with intent and outcome, `log`, `journal`, `note`.
- Memory-guarded builds and jobs: `build`, `check`, `job run`, `job wait`, `worker`, under a shared
  budget.
- Integrity: a cached per-module axiom audit, import-layer checks, `rules` (agent rule files in
  step), and `hook pre-commit` for git.
- Client integration: an MCP server (`lockstep-mcp`) exposing every command; Claude Code hooks
  (`hook session-start`, `hook pre-bash`).
- Experimental: durable operations, proof capture/check/prove, `node finish`, finite probes,
  `context`, the research-library catalog, certificate layers and `storage share`.
