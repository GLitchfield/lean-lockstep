from __future__ import annotations

import json
import re
import time
from pathlib import Path

from lockstep import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE
from lockstep.build import run_guarded
from lockstep.context import Ctx
from lockstep.errors import CheckFailed, LeanError, UsageError, Refused
from lockstep.lock import lock_entry, read_lock, write_lock
from lockstep.modules import module_to_path, scan_modules, transitive_closure
from lockstep.nodes import (find_node, load_nodes_json, local_name, node_module_name, nodes_from_json, qualify,
                      resolve_depends)
from lockstep.output import Result, table
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store
from lockstep.tags import KINDS, render_docstring


# ---------------------------------------------------------------------------
# helpers


def _root_imports(cfg) -> list[str]:
    text = cfg.blueprint_root.read_text() if cfg.blueprint_root.exists() else ""
    return re.findall(r"^import\s+([\w.]+)", text, re.M)


def add_root_import(cfg, module: str) -> None:
    text = cfg.blueprint_root.read_text() if cfg.blueprint_root.exists() else ""
    if re.search(rf"^import\s+{re.escape(module)}\s*$", text, re.M):
        return
    text = text.rstrip("\n") + f"\nimport {module}\n"
    cfg.blueprint_root.write_text(text)


def remove_root_import(cfg, module: str) -> None:
    if not cfg.blueprint_root.exists():
        return
    text = cfg.blueprint_root.read_text()
    text = re.sub(rf"^import\s+{re.escape(module)}\s*\n", "", text, flags=re.M)
    cfg.blueprint_root.write_text(text)


def render_module(cfg, *, short: str, signature: str, docstring: str, imp: str, header: str = "") -> str:
    """The node module. `short` is the declaration name relative to the declaration namespace
    (`local_name`). When the core root uses Lean's module system the node is written in module
    form from the start, as `lockstep close` would leave it, so it elaborates against the same
    imports in the blueprint as after the move into the core library."""
    from lockstep.commands.close import core_uses_module_system, to_module_form

    tmpl = cfg.node_template.read_text()
    text = (
        tmpl.replace("{{HEADER}}", header)
        .replace("{{IMPORT}}", imp)
        .replace("{{NAMESPACE}}", cfg.declaration_namespace)
        .replace("{{DOCSTRING}}", docstring)
        .replace("{{SHORT}}", short)
        .replace("{{SIGNATURE}}", signature.strip())
    )
    if core_uses_module_system(cfg):
        text = to_module_form(text) or text
    return text


def elaborate_module(ctx: Ctx, module: str, budget: int = 20) -> None:
    """`lake build <module>` under the guard; raise LeanError with the log tail on failure."""
    cfg = ctx.cfg
    log = cfg.build_log_dir / f"elab-{time.strftime('%Y%m%d-%H%M%S')}-{module}.log"
    res = run_guarded(cfg, [cfg.lake, "build", module], log_path=log, budget_min=budget)
    if res.refused:
        raise Refused(f"{module} elaboration refused ({res.refused})")
    if not res.ok:
        raise LeanError(f"{module} failed to elaborate ({res.summary()}):\n" + "\n".join(res.tail))


def closure_olean_gib(cfg, module: str) -> float:
    """Project olean bytes (all `.olean*` parts) in the import closure of ``module``."""
    mods = scan_modules(cfg)
    lib = cfg.root / ".lake" / "build" / "lib" / "lean"
    total = 0
    # exact paths, not a glob per module: a closure of thousands of certificate modules
    # against a directory of tens of thousands of files made the glob quadratic
    for m in transitive_closure(mods, [module]):
        base = lib / module_to_path(m).with_suffix("")
        for suffix in (".olean", ".olean.hash", ".olean.private", ".olean.server", ".ilean"):
            f = base.with_name(base.name + suffix)
            if f.exists():
                total += f.stat().st_size
    return total / 2**30


def targeted_extract(ctx: Ctx, module: str):
    """Extract over ``module`` and its import closure only, under the targeted cap.

    Used when the full extractor is refused for memory: enough to hash the new statement
    and write its lock entry. The duplicate check is then limited to the closure, and
    nodes.json is not rewritten (the next full `lockstep extract` does both)."""
    from lockstep.commands.extract import run_extract

    cfg = ctx.cfg
    gib = closure_olean_gib(cfg, module)
    if gib > cfg.extract_targeted_max_olean_gib:
        raise Refused(f"import closure of {module} holds {gib:.1f} GiB of oleans; "
                      f"above extract.targeted_max_olean_gib = {cfg.extract_targeted_max_olean_gib}")
    tmp = cfg.state_home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return run_extract(ctx, no_cache=True, write=False, imports=[module],
                       memory_max=cfg.extract_targeted_memory_max, script=tmp / "ExtractTargeted.lean")


def re_extract(ctx: Ctx):
    from lockstep.commands.extract import run_extract

    payload, index, problems = run_extract(ctx, no_cache=False)
    return payload, index, problems


def append_log(path: Path, title: str, body: dict) -> None:
    lines = [f"\n## {time.strftime('%Y-%m-%d')} {title}\n"]
    for k, v in body.items():
        if v in (None, "", []):
            continue
        if isinstance(v, str) and "\n" in v:
            lines.append(f"- {k}:\n\n```\n{v}\n```\n")
        else:
            lines.append(f"- {k}: {v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# show


def run_show(ctx: Ctx, name: str) -> Result:
    cfg = ctx.cfg
    data = load_nodes_json(cfg)
    n = find_node(cfg, name, nodes_from_json(data))
    lock = read_lock(cfg.lock_file).get(n.name)
    with Store(cfg) as st:
        claim = st.claims().get(n.name)
        attempts = [dict(a) for a in st.attempts(n.name)]
        jobs = [j for j in st.jobs(limit=200) if j.get("node") == n.name][:3]
    consumer_ok = None
    if n.consumer:
        idx_names = {r["name"] for r in json.loads(cfg.index_json.read_text())} if cfg.index_json.exists() else set()
        consumer_ok = n.consumer in idx_names or any(x["name"] == n.consumer for x in data.get("nodes", []))
    res = Result()
    res.line(f"{n.name}  [{n.kind}, ws {n.ws}]  status: {n.status}{' (ready)' if n.ready else ''}")
    res.line(f"module: {n.module}  ({module_to_path(n.module)})")
    res.line(f"opened: {n.opened or '?'}")
    res.line("")
    res.line(n.description)
    res.line("")
    res.line("statement:")
    res.human.extend("  " + line for line in n.type_pp.splitlines())
    res.line(f"type hash: {n.type_hash}   lock: {'matches' if n.lock_ok else ('MISSING' if lock is None else 'MISMATCH')}" + (f" (approved by {lock.get('approved_by')} on {lock.get('approved_at')})" if lock else ""))
    res.line(f"sorry: {n.sorry}   axioms: {', '.join(n.axioms) or '-'}" + (f"   uncovered: {', '.join(n.uncovered_axioms)}" if n.uncovered_axioms else ""))
    res.line(f"declared deps: {', '.join(n.deps_declared) or '-'}")
    res.line(f"actual deps:   {', '.join(n.deps_actual) or '-'}")
    if n.blocked_by:
        res.line(f"blocked by: {', '.join(n.blocked_by)}")
    res.line(f"consumer: {n.consumer or ('(dangling-ok: ' + str(n.tags.get('dangling-ok')) + ')')}" + ("" if consumer_ok is None else (" [exists]" if consumer_ok else " [NOT FOUND in index]")))
    if n.falsifier:
        res.line(f"falsifier: {n.falsifier}  result: {(n.falsify or {}).get('result', 'not run')}" + (f" at {(n.falsify or {}).get('at')}" if n.falsify else ""))
    for k in ("measure", "target"):
        if n.tags.get(k):
            res.line(f"{k}: {n.tags[k]}")
    if n.helpers:
        res.line(f"helpers: {', '.join(n.helpers)}")
    if n.tag_errors:
        res.line("tag errors: " + "; ".join(n.tag_errors))
    res.line(f"claim: {claim['agent'] + ' until ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(claim['expires_at'])) if claim else 'none'}")
    if attempts:
        res.line(f"attempts: {len(attempts)} (" + ", ".join(str(a.get('outcome') or 'live') for a in attempts[-5:]) + ")")
    if jobs:
        j = jobs[0]
        res.line(f"last job: {j['id']} {j['kind']} {j['status']} rc={j.get('exit_code')}")
    res.payload = {"node": n.to_json(), "lock": lock, "claim": claim, "attempts": attempts, "jobs": jobs, "consumer_exists": consumer_ok}
    res.exit_code = EXIT_OK
    return res


# ---------------------------------------------------------------------------
# add


def run_add(ctx: Ctx, name: str, ws: str, kind: str, type: str, description: str, depends: list[str] | None = None,
            consumer: str | None = None, dangling_ok: str | None = None, falsifier: str | None = None,
            approved_by: str | None = None, measure: str | None = None, target: str | None = None,
            import_: str | None = None, force: bool = False, no_search: bool = False,
            targeted: bool = False) -> Result:
    cfg = ctx.cfg
    if ws not in cfg.workstreams:
        raise UsageError(f"unknown workstream {ws}; configured: {', '.join(cfg.workstreams)}")
    if kind not in KINDS:
        raise UsageError(f"kind must be one of {', '.join(KINDS)}")
    if not consumer and not dangling_ok:
        raise UsageError("give --consumer <theorem> or --dangling-ok <reason>")
    if kind == "conjecture" and not falsifier:
        raise UsageError("conjecture nodes need --falsifier <script under harness/falsifiers/>")
    full = qualify(cfg, name)
    short = local_name(cfg, full)
    module = node_module_name(cfg, full, ws)
    path = cfg.root / module_to_path(module)
    tags = {"ws": ws, "kind": kind, "depends": resolve_depends(cfg, depends or []),
            "consumer": qualify(cfg, consumer) if consumer else None, "dangling-ok": dangling_ok,
            "falsifier": falsifier, "measure": measure, "target": target}
    doc = render_docstring(description, tags)
    res = Result()
    # near-duplicate search on description and type
    if not no_search:
        with Store(cfg) as st:
            hits = st.search(" OR ".join(re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", description)[:12]) or description, limit=5)
        if hits:
            res.line("nearest existing declarations (check these are not the same statement):")
            res.human.extend(table([[h["name"], h["module"], (h["doc"] or "").splitlines()[0][:70] if h["doc"] else ""] for h in hits]))
    text = render_module(cfg, short=short, signature=type, docstring=doc, imp=import_ or cfg.node_default_import)
    # Re-running the same add (for example after the extractor was refused for memory) is
    # allowed when the module on disk is exactly what this call would write.
    if path.exists() and not force and path.read_text() != text:
        raise UsageError(f"{path.relative_to(cfg.root)} already exists with different content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    add_root_import(cfg, module)
    try:
        elaborate_module(ctx, module)
        if targeted:
            payload, index, problems = targeted_extract(ctx, module)
        else:
            try:
                payload, index, problems = re_extract(ctx)
            except Refused as exc:
                # The full extractor cannot start (memory). Hash the statement from this module's
                # import closure alone; the full extract runs later.
                res.line(f"full extract refused ({exc}); falling back to a targeted extract of {module}")
                payload, index, problems = targeted_extract(ctx, module)
                targeted = True
    except Refused as exc:
        # Nothing ran: keep the elaborated module and the root import so the same command
        # can be repeated once memory allows; no lock entry exists until then.
        raise Refused(f"{exc}\nmodule kept at {path.relative_to(cfg.root)} (no lock entry yet); "
                      f"repeat this exact `lockstep node add` when memory allows") from exc
    except LeanError:
        path.unlink(missing_ok=True)
        remove_root_import(cfg, module)
        raise
    node = next((x for x in payload["nodes"] if x["name"] == full), None)
    if node is None:
        path.unlink(missing_ok=True)
        remove_root_import(cfg, module)
        raise LeanError(f"{full} did not appear in the extractor output; module {module}")
    dup = [p for p in payload.get("duplicate_problems", []) if p.startswith(full + " ")]
    if dup and not force:
        path.unlink(missing_ok=True)
        remove_root_import(cfg, module)
        if not targeted:
            re_extract(ctx)
        raise CheckFailed("refused: " + "; ".join(dup))
    lock = read_lock(cfg.lock_file)
    lock[full] = lock_entry(node["type_hash"], node["type_pp"], approved_by or ctx.agent, kind, ws) | {"module": module}
    write_lock(cfg.lock_file, lock)
    if not targeted:
        re_extract(ctx)
    with Store(cfg) as st:
        st.event("node-added", full, ctx.agent, {"module": module, "kind": kind, "ws": ws, "targeted": targeted})
    res.line(f"added {full} in {path.relative_to(cfg.root)} (status {node['status']}); lock entry written")
    if targeted:
        res.line(f"targeted extract: duplicate check covered only the import closure of {module}; "
                 f"nodes.json/status.md not updated; run `lockstep extract` when memory allows")
    for p in payload.get("problems", []):
        if p.startswith(full):
            res.line(f"  note: {p}")
    res.payload = {"node": node, "module": module, "path": str(path.relative_to(cfg.root))}
    res.exit_code = EXIT_OK
    return res


# ---------------------------------------------------------------------------
# restate


def run_restate(ctx: Ctx, name: str, type: str, approved_by: str | None = None, reason: str | None = None,
                description: str | None = None) -> Result:
    cfg = ctx.cfg
    n = find_node(cfg, name)
    with Store(cfg) as st:
        claim = st.claims().get(n.name)
    if claim and claim["agent"] != ctx.agent:
        raise CheckFailed(f"{n.name} is claimed by {claim['agent']}; cannot restate")
    path = cfg.root / module_to_path(n.module)
    text = path.read_text()
    decl = local_name(cfg, n.name)
    pat = re.compile(rf"(theorem\s+{re.escape(decl)}\b)(.*?)(:=\s*by\b)", re.S)
    m = pat.search(text)
    if not m:
        raise UsageError(f"cannot find `theorem {decl} ... := by` in {path.relative_to(cfg.root)}")
    old_sig = m.group(2).strip()
    new_text = text[: m.start(2)] + " " + type.strip() + " " + text[m.start(3):]
    if description is not None:
        new_text = re.sub(r"/--.*?-/", "/-- " + render_docstring(description, n.tags) + " -/", new_text, count=1, flags=re.S)
    backup = text
    path.write_text(new_text)
    try:
        elaborate_module(ctx, n.module)
        payload, index, problems = re_extract(ctx)
    except LeanError:
        path.write_text(backup)
        raise
    node = next((x for x in payload["nodes"] if x["name"] == n.name), None)
    if node is None:
        path.write_text(backup)
        raise LeanError(f"{n.name} vanished after restatement")
    lock = read_lock(cfg.lock_file)
    old = lock.get(n.name, {})
    lock[n.name] = lock_entry(node["type_hash"], node["type_pp"], approved_by or ctx.agent, n.kind, n.ws) | {"module": n.module}
    write_lock(cfg.lock_file, lock)
    append_log(cfg.restatements_file, f"restate {n.name}", {
        "approved_by": approved_by or ctx.agent, "reason": reason,
        "old_type_hash": old.get("type_hash"), "new_type_hash": node["type_hash"],
        "old": old.get("type_pp") or old_sig, "new": node["type_pp"],
    })
    re_extract(ctx)
    with Store(cfg) as st:
        st.event("node-restated", n.name, ctx.agent, {"reason": reason})
    return Result(EXIT_OK, {"node": node}, [f"restated {n.name}; lock updated; entry appended to {cfg.restatements_file.relative_to(cfg.root)}"])


# ---------------------------------------------------------------------------
# retire


def run_retire(ctx: Ctx, name: str, reason: str, as_refutation: bool = False, approved_by: str | None = None) -> Result:
    cfg = ctx.cfg
    n = find_node(cfg, name)
    path = cfg.root / module_to_path(n.module)
    outcome = "refuted" if as_refutation else "retired"
    if as_refutation:
        text = path.read_text()
        pat = re.compile(rf"(theorem\s+{re.escape(local_name(cfg, n.name))}\b)(.*?)(:=\s*by\b)", re.S)
        m = pat.search(text)
        if not m:
            raise UsageError(f"cannot rewrite {path.relative_to(cfg.root)} as a refutation")
        sig = m.group(2)
        # binders stay; the conclusion after the last top-level ':' is negated
        idx = sig.rfind(":")
        new_sig = sig[: idx + 1] + " ¬(" + sig[idx + 1 :].strip() + ")"
        tags = dict(n.tags)
        tags["kind"] = "refutation"
        new_text = text[: m.start(2)] + new_sig + " " + text[m.start(3):]
        new_text = re.sub(r"/--.*?-/", "/-- " + render_docstring(n.description, tags) + " -/", new_text, count=1, flags=re.S)
        path.write_text(new_text)
        try:
            elaborate_module(ctx, n.module)
        except LeanError:
            path.write_text(text)
            raise
    else:
        from lockstep.gitutil import git

        git(cfg, "rm", "-q", "--cached", str(path.relative_to(cfg.root)), check=False)
        path.unlink(missing_ok=True)
        remove_root_import(cfg, n.module)
    append_log(cfg.retired_file, f"{outcome} {n.name}", {"reason": reason, "approved_by": approved_by or ctx.agent,
                                                          "statement": n.type_pp, "module": n.module, "kind": n.kind, "ws": n.ws})
    with Store(cfg) as st:
        st.release(n.name, ctx.agent, outcome=outcome)
        st.record_attempt(n.name, ctx.agent, outcome, note=reason)
        st.event(outcome, n.name, ctx.agent, {"reason": reason})
    payload, index, problems = re_extract(ctx)
    if as_refutation:
        node = next((x for x in payload["nodes"] if x["name"] == n.name), None)
        if node:
            lock = read_lock(cfg.lock_file)
            lock[n.name] = lock_entry(node["type_hash"], node["type_pp"], approved_by or ctx.agent, "refutation", n.ws) | {"module": n.module}
            write_lock(cfg.lock_file, lock)
            re_extract(ctx)
    else:
        lock = read_lock(cfg.lock_file)
        if n.name in lock:
            lock[n.name]["retired"] = time.strftime("%Y-%m-%d")
            write_lock(cfg.lock_file, lock)
            re_extract(ctx)
    return Result(EXIT_OK, {"node": n.name, "outcome": outcome}, [f"{outcome} {n.name}; logged in {cfg.retired_file.relative_to(cfg.root)}"])


command(CommandSpec(name="node_show", cli=["node", "show"], help="Everything known about a node.", run=run_show,
                    args=[Arg("name", "node name (short or fully qualified)", positional=True, required=True)]))
command(CommandSpec(
    name="node_add", cli=["node", "add"], long_running=True,
    help="Create a node module with a `sorry` proof, elaborate it, refuse duplicates, add it to the blueprint root and the lock.",
    run=run_add,
    args=[
        Arg("name", "declaration name (short names are qualified with the declaration namespace)", required=True),
        Arg("ws", "workstream letter", required=True),
        Arg("kind", "formalization | conjecture | refutation", required=True, choices=list(KINDS)),
        Arg("type", "binders and type, e.g. \"(k : ℕ) (hk : 3 ≤ k) : ForcesConvexPolygon k (2 ^ (k - 2) + 1)\"", required=True),
        Arg("description", "natural-language description (docstring body)", required=True),
        Arg("depends", "comma-separated project declarations (nodes, core theorems) the proof needs; readiness waits on them", "list"),
        Arg("consumer", "existing theorem that consumes this node"),
        Arg("dangling_ok", "reason for having no consumer"),
        Arg("falsifier", "script under harness/falsifiers/ (required for conjectures)"),
        Arg("approved_by", "who approved the statement (default: the agent)"),
        Arg("measure", "decreasing measure for induction-shaped nodes"),
        Arg("target", "core module the closed node should move under"),
        Arg("import_", "module the node file imports (default from config)"),
        Arg("force", "overwrite an existing module / keep an exact duplicate", "bool"),
        Arg("no_search", "skip the near-duplicate search", "bool"),
        Arg("targeted", "extract only the new node's import closure; do not rewrite the full index", "bool"),
    ],
))
command(CommandSpec(
    name="node_restate", cli=["node", "restate"], long_running=True,
    help="Rewrite a node's statement in place, update the lock, append to restatements.md.",
    run=run_restate,
    args=[Arg("name", "node name", positional=True, required=True), Arg("type", "new binders and type", required=True),
          Arg("approved_by", "who approved"), Arg("reason", "why the statement changed"), Arg("description", "new docstring body")],
))
command(CommandSpec(
    name="node_retire", cli=["node", "retire"], long_running=True,
    help="Delete a node (or rewrite it as a ¬-refutation), log it in retired.md, record the attempt outcome.",
    run=run_retire,
    args=[Arg("name", "node name", positional=True, required=True), Arg("reason", "why", required=True),
          Arg("as_refutation", "keep the module, negate the conclusion, kind=refutation", "bool"), Arg("approved_by", "who decided")],
))
