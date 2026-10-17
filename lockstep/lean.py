"""Materialize and run the Lean extractor; load its JSON output."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from lockstep.build import BuildResult, run_guarded
from lockstep.config import Config
from lockstep.errors import LeanError, Refused
from lockstep.modules import Module, scan_modules


def render(cfg: Config, script: Path | None = None, imports: list[str] | None = None) -> tuple[Path, str]:
    """The extractor script's path and text, from the template."""
    tmpl = cfg.extract_template.read_text()
    imports = imports or cfg.extract_import_modules
    script = script or cfg.extract_script
    text = (
        tmpl.replace("{{SCRIPT}}", str(script.relative_to(cfg.root)) if script.is_relative_to(cfg.root) else str(script))
        .replace("{{DEFAULT_IMPORTS}}", ", ".join(f"`{m}" for m in imports))
    )
    return script, text


def extractor_digest(cfg: Config, script: Path | None = None, imports: list[str] | None = None) -> str:
    """Identity of the extractor program. It enters the extract cache key, so a harness upgrade that
    changes the extractor does not keep serving the old extractor's output."""
    return hashlib.sha256(render(cfg, script, imports)[1].encode()).hexdigest()[:12]


def materialize(cfg: Config, script: Path | None = None, imports: list[str] | None = None) -> Path:
    script, text = render(cfg, script, imports)
    script.parent.mkdir(parents=True, exist_ok=True)
    if not script.exists() or script.read_text() != text:
        script.write_text(text)
    return script


@dataclass
class ExtractOutput:
    nodes: dict
    index: list[dict]
    result: BuildResult


def run_extractor(
    cfg: Config,
    *,
    out: Path,
    index: Path,
    mods: dict[str, Module] | None = None,
    imports: list[str] | None = None,
    script: Path | None = None,
    no_taint: bool = False,
    budget_min: float | None = None,
    force: bool = False,
    memory_max: str | None = None,
) -> ExtractOutput:
    script = materialize(cfg, script, imports)
    mods = mods if mods is not None else scan_modules(cfg)
    # Keep the blueprint oleans current so the environment reflects the node sources
    # (adding a node changes the root's imports; editing a proof changes its olean).
    # A targeted run (explicit imports) only needs those modules built.
    bp_targets = list(imports) if imports else cfg.build_targets("blueprint")
    bp_log = cfg.build_log_dir / f"blueprint-{time.strftime('%Y%m%d-%H%M%S')}.log"
    bp = run_guarded(cfg, [cfg.lake, "build", *bp_targets], log_path=bp_log, budget_min=cfg.core_budget_min * 2, force=force)
    if bp.refused:
        raise Refused(f"blueprint build refused before extraction ({bp.refused})")
    if not bp.ok:
        raise LeanError(f"blueprint build failed before extraction ({bp.summary()}):\n" + "\n".join(bp.tail))
    index_layers = set(cfg.extract_index_layers)
    index_modules = sorted(m.name for m in mods.values() if m.layer in index_layers and not m.generated)
    tmp = cfg.state_home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    idx_file = tmp / "index-modules.txt"
    idx_file.write_text("\n".join(index_modules) + "\n")
    argv = [cfg.lake, "env", "lean", "--run", str(script),
            "--out", str(out), "--index", str(index),
            "--prefix", cfg.blueprint_namespace,
            "--index-modules-file", str(idx_file)]
    if cfg.destinations_file.exists():
        argv += ["--destinations-file", str(cfg.destinations_file)]
    if not no_taint:
        argv += ["--taint-dir", str(cfg.taint_dir)]
    else:
        argv += ["--no-taint"]
    for ax in cfg.standard_axioms:
        argv += ["--standard", ax]
    for m in imports or []:
        argv += ["--import", m]
    log = cfg.build_log_dir / f"extract-{time.strftime('%Y%m%d-%H%M%S')}.log"
    res = run_guarded(cfg, argv, log_path=log, budget_min=budget_min or cfg.extract_budget_min,
                      memory_max=memory_max or cfg.extract_memory_max, force=force, count_built=False)
    if res.refused:
        raise Refused(f"extractor refused ({res.refused}); set LOCKSTEP_EXTRACT_MEMORY_MAX to lower the cap once the peak is known")
    if not res.ok:
        raise LeanError(f"extractor failed ({res.summary()}):\n" + "\n".join(res.tail))
    try:
        nodes = json.loads(out.read_text())
        idx = json.loads(index.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LeanError(f"extractor wrote unreadable output: {exc}") from exc
    return ExtractOutput(nodes=nodes, index=idx, result=res)
