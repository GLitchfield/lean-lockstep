from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from lockstep import EXIT_CHECK_FAILED, EXIT_OK
from lockstep.build import available_gib, lean_compilers_running
from lockstep.config import validate
from lockstep.context import Ctx
from lockstep.gitutil import git_ok, head_sha
from lockstep.modules import module_to_path, scan_modules
from lockstep.output import Result
from lockstep.spec import CommandSpec, command


def run(ctx: Ctx) -> Result:
    cfg = ctx.cfg
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    tc = cfg.root / "lean-toolchain"
    add("lean-toolchain", tc.exists(), tc.read_text().strip() if tc.exists() else "missing")
    lake = shutil.which(cfg.lake)
    ver = ""
    if lake:
        try:
            ver = subprocess.run([cfg.lake, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            ver = "?"
    add("lake", bool(lake), ver or f"{cfg.lake} not on PATH")
    try:
        validate(cfg)
        add("config", True, str(cfg.root / "harness/config.toml"))
    except Exception as exc:  # noqa: BLE001
        add("config", False, str(exc))
    add("git", git_ok(cfg), f"HEAD {head_sha(cfg)[:12]}" + (f" (git-dir {cfg.git_dir})" if cfg.git_dir else ""))
    try:
        from lockstep.store import Store
        with Store(cfg) as st:
            add("database", True, f"{cfg.db_path} ({st.table_counts()})")
    except Exception as exc:  # noqa: BLE001
        add("database", False, str(exc))
    lib = cfg.root / ".lake" / "build" / "lib" / "lean"
    mods = scan_modules(cfg)
    cert_layer = cfg.certificates_layer
    if cert_layer:
        certs = [m for m in mods.values() if m.layer == cert_layer]
        missing = [m.name for m in certs if not (lib / module_to_path(m.name)).with_suffix(".olean").exists()]
        add("certificate oleans", not missing, f"{len(certs) - len(missing)}/{len(certs)} present" + (f"; missing e.g. {missing[:3]}" if missing else ""))
    from lockstep.modules import transitive_closure
    closure = transitive_closure(mods, [cfg.core_root_module])
    core = [m for m in mods.values() if m.layer == cfg.core_layer and m.name in closure]
    missing_core = [m.name for m in core if not (lib / module_to_path(m.name)).with_suffix(".olean").exists()]
    outside = [m.name for m in mods.values() if m.layer == cfg.core_layer and m.name not in closure]
    add("core oleans", not missing_core, f"{len(core) - len(missing_core)}/{len(core)} present in the root closure" + (f"; missing {missing_core[:5]}" if missing_core else "") + (f"; {len(outside)} core module(s) outside the root closure" if outside else ""))
    add("blueprint root", cfg.blueprint_root.exists(), str(cfg.blueprint_root))
    add("extract template", cfg.extract_template.exists(), str(cfg.extract_template))
    add("systemd-run guard", (not cfg.build_guard) or shutil.which("systemd-run") is not None, "guard enabled" if cfg.build_guard else "guard disabled")
    add("memory", available_gib() > 8, f"{available_gib():.0f} GiB available; {lean_compilers_running()} lean compiler(s) running")
    from lockstep.build import _mem_gib
    add('full extraction admission', _mem_gib(cfg.extract_memory_max) <= _mem_gib(cfg.jobs_memory_budget),
        f'effective extract cap {cfg.extract_memory_max}; shared budget {cfg.jobs_memory_budget}; use an explicit smaller stage cap after measuring its peak')
    for name, path in (("destinations", cfg.destinations_file), ("allowlist", cfg.allowlist_file), ("lock", cfg.lock_file)):
        add(name, path.exists(), str(path))
    script = cfg.extract_script
    if script.exists():
        head = script.read_text()[:3000]
        bad = [line for line in head.splitlines() if line.startswith("import ") and line.split()[1] != "Lean"]
        add("extractor imports", not bad, "only `import Lean` at the top (a second load of the project oleans cannot mmap and reads ~37 GB into RAM)" if not bad else f"top-level {bad[0]!r}: remove it, main imports the project")
    cache = cfg.taint_dir
    n_cache = len(list(cache.glob("*.json"))) if cache.exists() else 0
    add("taint cache", True, f"{n_cache} module file(s) in {cache}")
    nodes = cfg.nodes_json
    add("nodes.json", nodes.exists(), str(nodes) if nodes.exists() else "not extracted yet")
    res = Result()
    for name, ok, detail in checks:
        res.line(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    res.exit_code = EXIT_OK if all(ok for _, ok, _ in checks) else EXIT_CHECK_FAILED
    res.payload = {"checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]}
    return res


command(CommandSpec(name="doctor", cli=["doctor"], help="Check toolchain, lake, config, database, oleans, and cache health.", run=run))
