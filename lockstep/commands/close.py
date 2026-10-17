from __future__ import annotations

import re
import time

from lockstep import EXIT_OK
from lockstep.build import run_guarded
from lockstep.context import Ctx
from lockstep.errors import CheckFailed, LeanError
from lockstep.gitutil import git_mv
from lockstep.lock import read_lock, write_lock
from lockstep.modules import module_to_path, read_name_list, write_name_list
from lockstep.nodes import camel, find_node
from lockstep.output import Result
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def rewrite_imports(cfg, old: str, new: str, dirs: list[str]) -> list[str]:
    touched = []
    pat = re.compile(rf"^(\s*(?:public\s+)?(?:private\s+)?import\s+(?:all\s+)?){re.escape(old)}(\s*$)", re.M)
    for d in dirs:
        base = cfg.root / d
        files = [base] if base.is_file() else sorted(base.rglob("*.lean")) if base.is_dir() else []
        for p in files:
            text = p.read_text(errors="replace")
            if old not in text:
                continue
            new_text = pat.sub(rf"\g<1>{new}\g<2>", text)
            if new_text != text:
                p.write_text(new_text)
                touched.append(str(p.relative_to(cfg.root)))
    return touched


def run(ctx: Ctx, name: str, dry_run: bool = False, force: bool = False) -> Result:
    cfg = ctx.cfg
    n = find_node(cfg, name)
    with Store(cfg) as st:
        claim = st.claims().get(n.name)
    if claim and claim["agent"] != ctx.agent and not force:
        raise CheckFailed(f"{n.name} is claimed by {claim['agent']}")
    if n.sorry:
        raise CheckFailed(f"{n.name} still depends on sorry")
    if n.uncovered_axioms:
        raise CheckFailed(f"{n.name} reaches un-allowlisted axiom(s): {', '.join(n.uncovered_axioms)}")
    if n.status not in ("closed",):
        raise CheckFailed(f"{n.name} has status {n.status}; run `lockstep check {n.short}` first")
    target_ns = n.tags.get("target") or f"{cfg.closed_target_default}.{n.ws}"
    new_module = f"{target_ns}.{camel(n.short)}"
    old_path = cfg.root / module_to_path(n.module)
    new_path = cfg.root / module_to_path(new_module)
    consumer = n.consumer
    dirs = [str(cfg.blueprint_root.relative_to(cfg.root)), cfg.blueprint_namespace] + [p.replace(".", "/") for layer in cfg.layers() for p in layer.prefixes] + [p.replace(".", "/") + ".lean" for layer in cfg.layers() for p in layer.prefixes]
    plan = [f"git mv {old_path.relative_to(cfg.root)} {new_path.relative_to(cfg.root)}",
            f"rewrite `import {n.module}` -> `import {new_module}` under {', '.join(sorted(set(d for d in dirs if (cfg.root / d).exists())))}",
            f"remove `import {n.module}` from {cfg.blueprint_root.name}",
            f"append {n.name} to {cfg.destinations_file.relative_to(cfg.root)}",
            "lockstep extract",
            f"lake build {consumer_module(cfg, consumer) if consumer else '(no consumer)'}"]
    res = Result()
    if dry_run:
        res.human.extend(plan)
        res.payload = {"plan": plan, "new_module": new_module}
        res.exit_code = EXIT_OK
        return res
    if new_path.exists():
        raise CheckFailed(f"{new_path.relative_to(cfg.root)} already exists")
    git_mv(cfg, old_path, new_path)
    # the moved file keeps its content (the docstring tags document provenance), but a core
    # root written in Lean's module system cannot import a plain-`import` file
    if core_uses_module_system(cfg):
        converted = to_module_form(new_path.read_text())
        if converted is not None:
            new_path.write_text(converted)
    # nodes import the core root by default, and the core root is about to import the moved
    # module: replace that import by the root's own imports, which give the same environment
    core_root = cfg.root / module_to_path(cfg.core_root_module)
    if core_root.exists():
        inlined = inline_root_import(new_path.read_text(), cfg.core_root_module, core_root.read_text())
        if inlined is not None:
            new_path.write_text(inlined)
    touched = rewrite_imports(cfg, n.module, new_module, [d for d in dirs if (cfg.root / d).exists()])
    root_text = cfg.blueprint_root.read_text()
    root_text = re.sub(rf"^import\s+{re.escape(new_module)}\s*\n", "", root_text, flags=re.M)
    cfg.blueprint_root.write_text(root_text)
    dests = read_name_list(cfg.destinations_file)
    if n.name not in dests:
        write_name_list(cfg.destinations_file, dests + [n.name], header="Core-library theorems included in the axiom audit and the frontier (one per line).\n`lockstep close` appends every closed node automatically.")
    lock = read_lock(cfg.lock_file)
    if n.name in lock:
        lock[n.name]["merged_module"] = new_module
        lock[n.name]["merged_at"] = time.strftime("%Y-%m-%d")
        write_lock(cfg.lock_file, lock)
    # make sure the new core module is reachable from the core root when it has no other importer
    core_root = cfg.root / module_to_path(cfg.core_root_module)
    bp_root_rel = str(cfg.blueprint_root.relative_to(cfg.root))
    core_importers = [t for t in touched if t != bp_root_rel and not t.startswith(cfg.blueprint_namespace + "/")]
    if core_root.exists() and not core_importers:
        text = core_root.read_text()
        if not re.search(rf"^(?:public\s+)?import\s+{re.escape(new_module)}\s*$", text, re.M):
            kw = "public import" if re.search(r"^public import", text, re.M) else "import"
            core_root.write_text(text.rstrip("\n") + f"\n{kw} {new_module}\n")
            touched.append(str(core_root.relative_to(cfg.root)))
    log = cfg.build_log_dir / f"close-{time.strftime('%Y%m%d-%H%M%S')}-{n.short}.log"
    # the consumer may itself be a node closed a moment ago, whose module the index has
    # not recorded yet: fall back to its lock entry, then to the moved module itself
    build_target = consumer_module(cfg, consumer) if consumer else None
    if consumer and not build_target:
        build_target = lock.get(consumer, {}).get("merged_module") or lock.get(consumer, {}).get("module")
    if not build_target:
        build_target = new_module
    build = run_guarded(cfg, [cfg.lake, "build", build_target], log_path=log, budget_min=cfg.core_budget_min * 2, force=force)
    if not build.ok:
        raise LeanError(f"consumer build failed after the move ({build.summary()}); the move is left in place for inspection:\n" + "\n".join(build.tail))
    # the extractor reads the core root's environment: rebuild the root so that it contains
    # the moved module, otherwise the new destination is reported as not found
    if build_target != cfg.core_root_module:
        root_log = cfg.build_log_dir / f"close-{time.strftime('%Y%m%d-%H%M%S')}-{n.short}-root.log"
        root_build = run_guarded(cfg, [cfg.lake, "build", cfg.core_root_module], log_path=root_log,
                                 budget_min=cfg.core_budget_min * 2, force=force)
        if not root_build.ok:
            raise LeanError(f"core root build failed after the move ({root_build.summary()}); the move is left in place for inspection:\n" + "\n".join(root_build.tail))
    from lockstep.commands.extract import run_extract
    run_extract(ctx)
    with Store(cfg) as st:
        st.release(n.name, ctx.agent, outcome="closed")
        st.record_attempt(n.name, ctx.agent, "closed", note="lockstep close")
        st.event("closed", n.name, ctx.agent, {"module": new_module})
    res.line(f"closed {n.name}: moved to {new_path.relative_to(cfg.root)}; imports rewritten in {len(touched)} file(s); consumer build {build.summary()}")
    if consumer:
        res.line(f"suggested follow-up node: instantiate {consumer} with {n.name} (lockstep node add --consumer ...)")
    res.payload = {"node": n.name, "new_module": new_module, "touched": touched, "build": build.__dict__}
    res.exit_code = EXIT_OK
    return res


def inline_root_import(text: str, root_module: str, root_text: str) -> str | None:
    """Replace an import of `root_module` in `text` by the imports of `root_text` (the root's
    source), keeping the `public`/`private` prefix. Returns None when `text` does not import
    the root."""
    pat = re.compile(rf"^([ \t]*(?:public[ \t]+)?(?:private[ \t]+)?import[ \t]+(?:all[ \t]+)?){re.escape(root_module)}[ \t]*$", re.M)
    m = pat.search(text)
    if not m:
        return None
    targets = re.findall(r"^\s*(?:public\s+)?(?:private\s+)?import\s+(?:all\s+)?([\w.]+)\s*$", root_text, re.M)
    lines = "\n".join(m.group(1) + t for t in targets)
    return text[: m.start()] + lines + text[m.end():]


def core_uses_module_system(cfg) -> bool:
    """Whether the core root is written in Lean's module system (`module` header). Its imports
    must then be modules too, so nodes are written, and moved, in module form."""
    core_root = cfg.root / module_to_path(cfg.core_root_module)
    return core_root.exists() and re.match(r"\s*module\b", core_root.read_text()) is not None


def to_module_form(text: str) -> str | None:
    """Rewrite a plain-`import` Lean file as a module-system file: `module` header, `public`
    imports, one `@[expose] public section` around the body, closed by `end`.  Returns None
    when the file already starts with `module`."""
    if re.match(r"\s*module\b", text):
        return None
    lines = text.split("\n")
    imports = []
    while lines and (lines[0].startswith("import ") or lines[0].strip() == ""):
        line = lines.pop(0)
        if line.startswith("import "):
            imports.append("public " + line)
    body = "\n".join(lines).strip("\n")
    return "module\n\n" + "\n".join(imports) + "\n\n@[expose] public section\n\n" + body + "\n\nend\n"


def consumer_module(cfg, consumer: str | None) -> str | None:
    if not consumer:
        return None
    import json

    for path in (cfg.index_json, cfg.nodes_json):
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        recs = data if isinstance(data, list) else data.get("nodes", []) + data.get("destinations", [])
        for r in recs:
            if r.get("name") == consumer:
                return r.get("module")
    return None


command(CommandSpec(name="close", cli=["close"], long_running=True,
                    help="Move a proved, allowlisted node under the core library, rewrite imports, add it to destinations, rebuild its consumer.", run=run,
                    args=[Arg("name", "node name", positional=True, required=True), Arg("dry_run", "print the plan only", "bool"),
                          Arg("force", "ignore another agent's claim / running compilers", "bool")]))
