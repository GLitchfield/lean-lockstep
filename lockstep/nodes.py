"""Node model: extractor records + docstring tags -> statuses, readiness, status.md."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from lockstep.allowlist import read_allowlist, uncovered
from lockstep.config import Config
from lockstep.errors import UsageError
from lockstep.lock import read_lock
from lockstep.modules import module_to_path, read_name_list
from lockstep.tags import Tags, parse_docstring, validate_tags

STATUSES = ("proposed", "open", "claimed", "unallowlisted", "closed", "merged", "refuted", "retired")


@dataclass
class Node:
    name: str
    module: str
    type_pp: str
    type_hash: str
    sorry: bool
    axioms: list[str]
    nonstandard_axioms: list[str]
    deps_actual: list[str]
    description: str = ""
    tags: dict = field(default_factory=dict)
    tag_errors: list[str] = field(default_factory=list)
    helpers: list[str] = field(default_factory=list)
    # computed
    status: str = "open"
    ready: bool = False
    blocked_by: list[str] = field(default_factory=list)
    uncovered_axioms: list[str] = field(default_factory=list)
    claim: dict | None = None
    falsify: dict | None = None
    lock_ok: bool | None = None
    opened: str | None = None

    @property
    def ws(self) -> str:
        return str(self.tags.get("ws", "?"))

    @property
    def kind(self) -> str:
        return str(self.tags.get("kind", "formalization"))

    @property
    def deps_declared(self) -> list[str]:
        return list(self.tags.get("depends", []) or [])

    @property
    def consumer(self) -> str | None:
        return self.tags.get("consumer") or None

    @property
    def falsifier(self) -> str | None:
        return self.tags.get("falsifier") or None

    @property
    def short(self) -> str:
        return self.name.rsplit(".", 1)[-1]

    @property
    def path(self) -> Path:
        return module_to_path(self.module)

    def to_json(self) -> dict:
        return {
            "name": self.name, "module": self.module, "ws": self.ws, "kind": self.kind,
            "description": self.description, "tags": self.tags, "tag_errors": self.tag_errors,
            "type_pp": self.type_pp, "type_hash": self.type_hash, "sorry": self.sorry,
            "axioms": self.axioms, "nonstandard_axioms": self.nonstandard_axioms,
            "deps_declared": self.deps_declared, "deps_actual": self.deps_actual, "helpers": self.helpers,
            "status": self.status, "ready": self.ready, "blocked_by": self.blocked_by,
            "uncovered_axioms": self.uncovered_axioms, "falsify": self.falsify, "lock_ok": self.lock_ok,
            "opened": self.opened,
        }


@dataclass
class Destination:
    name: str
    module: str
    type_pp: str
    type_hash: str
    sorry: bool
    axioms: list[str]
    nonstandard_axioms: list[str]
    description: str = ""
    uncovered_axioms: list[str] = field(default_factory=list)
    merged_from: str | None = None

    def to_json(self) -> dict:
        return self.__dict__ | {}


# ---------------------------------------------------------------------------
# naming


def camel(short: str) -> str:
    parts = re.split(r"[_\s]+", short)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def node_module_name(cfg: Config, name: str, ws: str) -> str:
    return f"{cfg.blueprint_namespace}.{ws}.{camel(name.rsplit('.', 1)[-1])}"


def qualify(cfg: Config, name: str) -> str:
    ns = cfg.declaration_namespace
    return name if "." in name and name.startswith(ns + ".") else f"{ns}.{name}"


def local_name(cfg: Config, name: str) -> str:
    """The name as written in a node module, relative to the namespace the module opens:
    `Ns.List.foo` is declared as `theorem List.foo` inside `namespace Ns`."""
    return qualify(cfg, name)[len(cfg.declaration_namespace) + 1 :]


def project_names(cfg: Config) -> set[str]:
    """Declarations readiness can wait on: registered nodes (the last extract or the lock),
    core declarations (the index) and destinations."""
    names = set(read_lock(cfg.lock_file)) | set(read_name_list(cfg.destinations_file))
    if cfg.nodes_json.exists():
        names |= {n["name"] for n in json.loads(cfg.nodes_json.read_text()).get("nodes", [])}
    if cfg.index_json.exists():
        names |= {r["name"] for r in json.loads(cfg.index_json.read_text())}
    return names


def resolve_depends(cfg: Config, depends: list[str]) -> list[str]:
    """Resolve `--depends` names, as given or qualified, against `project_names`.

    A node is ready only once every declared dependency is closed, and a name the project
    does not know never is, so an unresolved name is refused instead of being recorded.
    Library lemmas the proof will use belong in the description."""
    if not depends:
        return []
    known = project_names(cfg)
    resolved, missing = [], []
    for d in depends:
        hit = next((c for c in (d, qualify(cfg, d)) if c in known), None)
        if hit:
            resolved.append(hit)
        else:
            missing.append(d)
    if missing:
        raise UsageError(f"--depends names no node, core declaration or destination: {', '.join(missing)}. "
                         "List only project declarations there (run `lockstep extract` if they are new); "
                         "name library lemmas in --description")
    return resolved


# ---------------------------------------------------------------------------
# falsifier results


def read_falsify_results(cfg: Config) -> dict[str, dict]:
    out: dict[str, dict] = {}
    d = cfg.falsify_results_dir
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("node") or p.stem
        out[name] = data
    return out


# ---------------------------------------------------------------------------
# building nodes from extractor output


def nodes_from_extract(cfg: Config, raw: dict) -> tuple[list[Node], list[Destination], list[str]]:
    """Group extractor declarations by module; the declaration tagged @node is the node."""
    problems: list[str] = []
    by_module: dict[str, list[dict]] = {}
    for rec in raw.get("declarations", []):
        by_module.setdefault(rec["module"], []).append(rec)
    nodes: list[Node] = []
    for module, recs in sorted(by_module.items()):
        mains = []
        for rec in recs:
            if rec.get("internal"):
                continue
            t = parse_docstring(rec.get("doc"))
            if t.is_node:
                mains.append((rec, t))
        if module == cfg.blueprint_namespace:
            continue
        if not mains:
            problems.append(f"{module}: no declaration carries @node")
            continue
        if len(mains) > 1:
            problems.append(f"{module}: {len(mains)} declarations carry @node ({', '.join(r['name'] for r, _ in mains)})")
        rec, t = mains[0]
        errs = validate_tags(t, cfg.workstreams)
        helpers = [r["name"] for r in recs if not r.get("internal") and r["name"] != rec["name"]]
        nodes.append(
            Node(
                name=rec["name"], module=module, type_pp=rec["type_pp"], type_hash=rec["type_hash"],
                sorry=bool(rec["sorry"]), axioms=list(rec.get("axioms", [])),
                nonstandard_axioms=list(rec.get("nonstandard_axioms", [])), deps_actual=list(rec.get("deps", [])),
                description=t.description, tags=dict(t.tags), tag_errors=errs, helpers=helpers,
            )
        )
        for e in errs:
            problems.append(f"{rec['name']}: {e}")
    dests: list[Destination] = []
    for rec in raw.get("destinations", []):
        t = parse_docstring(rec.get("doc"))
        dests.append(
            Destination(
                name=rec["name"], module=rec["module"], type_pp=rec["type_pp"], type_hash=rec["type_hash"],
                sorry=bool(rec["sorry"]), axioms=list(rec.get("axioms", [])),
                nonstandard_axioms=list(rec.get("nonstandard_axioms", [])), description=t.description,
            )
        )
    for missing in raw.get("missing_destinations", []):
        problems.append(f"destination {missing} not found in the environment")
    return nodes, dests, problems


# ---------------------------------------------------------------------------
# status computation


def compute(cfg: Config, nodes: list[Node], dests: list[Destination], core_names: set[str],
            claims: dict[str, dict] | None = None, falsify: dict[str, dict] | None = None,
            lock: dict | None = None, allow: list[dict] | None = None) -> None:
    claims = claims or {}
    falsify = falsify if falsify is not None else read_falsify_results(cfg)
    lock = lock if lock is not None else read_lock(cfg.lock_file)
    allow = allow if allow is not None else read_allowlist(cfg.allowlist_file)
    std = cfg.standard_axioms
    by_name = {n.name: n for n in nodes}
    dest_names = {d.name for d in dests}
    for d in dests:
        d.uncovered_axioms = uncovered(allow, d.name, d.nonstandard_axioms, std)
        if d.name in lock and d.name not in by_name:
            d.merged_from = lock[d.name].get("module")
    # first pass: intrinsic status
    for n in nodes:
        n.falsify = falsify.get(n.name)
        n.claim = claims.get(n.name)
        n.lock_ok = (n.name in lock and lock[n.name].get("type_hash") == n.type_hash)
        n.uncovered_axioms = uncovered(allow, n.name, n.nonstandard_axioms, std)
        fres = (n.falsify or {}).get("result")
        if fres == "counterexample":
            n.status = "refuted"
        elif not n.sorry:
            n.status = "closed" if not n.uncovered_axioms else "unallowlisted"
        elif n.kind == "conjecture" and fres != "pass":
            n.status = "proposed"
        elif n.name not in lock:
            n.status = "proposed"
        elif n.claim:
            n.status = "claimed"
        else:
            n.status = "open"
    # second pass: readiness
    for n in nodes:
        blocked = []
        for d in n.deps_declared:
            if d in by_name:
                if by_name[d].status not in ("closed", "merged"):
                    blocked.append(d)
            elif d in core_names or d in dest_names:
                continue
            else:
                blocked.append(f"{d} (unknown)")
        n.blocked_by = blocked
        n.ready = n.status == "open" and not n.claim and not blocked and (n.kind != "conjecture" or (n.falsify or {}).get("result") == "pass")


def duplicates(nodes: list[Node], index: list[dict]) -> list[str]:
    problems = []
    seen: dict[str, str] = {}
    for n in nodes:
        if n.type_hash in seen:
            problems.append(f"{n.name} duplicates {seen[n.type_hash]} (same statement)")
        seen[n.type_hash] = n.name
    core = {}
    for rec in index:
        if rec.get("kind") in ("theorem", "axiom") and not rec.get("internal"):
            core.setdefault(rec["type_hash"], rec["name"])
    for n in nodes:
        if n.type_hash in core:
            problems.append(f"{n.name} duplicates core theorem {core[n.type_hash]}; retire the node")
    return problems


# ---------------------------------------------------------------------------
# persistence


def load_nodes_json(cfg: Config) -> dict:
    p = cfg.nodes_json
    if not p.exists():
        raise UsageError(f"{p} not found; run `lockstep extract` first")
    return json.loads(p.read_text())


def nodes_from_json(data: dict) -> list[Node]:
    out = []
    for rec in data.get("nodes", []):
        n = Node(
            name=rec["name"], module=rec["module"], type_pp=rec["type_pp"], type_hash=rec["type_hash"],
            sorry=rec["sorry"], axioms=rec["axioms"], nonstandard_axioms=rec.get("nonstandard_axioms", []),
            deps_actual=rec.get("deps_actual", []), description=rec.get("description", ""), tags=rec.get("tags", {}),
            tag_errors=rec.get("tag_errors", []), helpers=rec.get("helpers", []),
        )
        n.status = rec.get("status", "open")
        n.ready = rec.get("ready", False)
        n.blocked_by = rec.get("blocked_by", [])
        n.uncovered_axioms = rec.get("uncovered_axioms", [])
        n.falsify = rec.get("falsify")
        n.lock_ok = rec.get("lock_ok")
        n.opened = rec.get("opened")
        out.append(n)
    return out


def dests_from_json(data: dict) -> list[Destination]:
    out = []
    for rec in data.get("destinations", []):
        out.append(Destination(**{k: rec.get(k) for k in ("name", "module", "type_pp", "type_hash", "sorry", "axioms", "nonstandard_axioms", "description", "uncovered_axioms", "merged_from")}))
    return out


def find_node(cfg: Config, name: str, nodes: list[Node] | None = None) -> Node:
    nodes = nodes if nodes is not None else nodes_from_json(load_nodes_json(cfg))
    full = qualify(cfg, name)
    for n in nodes:
        if n.name in (name, full):
            return n
    for n in nodes:
        if n.short == name:
            return n
    # A node registered from a targeted extract has a lock entry (with its module) before
    # the next full `lockstep extract` puts it into nodes.json: answer from the lock meanwhile.
    lock = read_lock(cfg.lock_file)
    for key, entry in lock.items():
        if key in (name, full) or key.rsplit(".", 1)[-1] == name:
            if entry.get("module"):
                # tags come from the module's docstring (the extractor's record is not available yet)
                tags: dict = {"ws": entry.get("ws", "?"), "kind": entry.get("kind", "formalization")}
                desc = "(not yet in nodes.json; run `lockstep extract`)"
                src = cfg.root / module_to_path(entry["module"])
                if src.exists():
                    m = re.search(r"/--(.*?)-/", src.read_text(errors="replace"), re.S)
                    if m:
                        t = parse_docstring(m.group(1))
                        if t.is_node:
                            tags = dict(t.tags) | {"ws": tags["ws"], "kind": tags["kind"]}
                            desc = t.description
                provisional = Node(name=key, module=entry["module"], type_pp=entry.get("type_pp", ""),
                            type_hash=entry.get("type_hash", ""), sorry=True, axioms=[], nonstandard_axioms=[],
                            deps_actual=[], description=desc, tags=tags, status="proposed")
                # Targeted falsification records evidence without replacing the full index.
                # Recompute readiness here so a newly registered conjecture can be claimed.
                index = json.loads(cfg.index_json.read_text()) if cfg.index_json.exists() else []
                evidence = read_falsify_results(cfg)
                record = evidence.get(key)
                if record and record.get("statement_type_hash") != provisional.type_hash:
                    evidence.pop(key)
                compute(cfg, nodes + [provisional], [], {r["name"] for r in index},
                        falsify=evidence)
                return provisional
    raise UsageError(f"unknown node {name}")


def node_module_for(cfg: Config, name: str) -> str:
    try:
        return find_node(cfg, name).module
    except UsageError:
        pass
    short = name.rsplit(".", 1)[-1]
    pat = re.compile(rf"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+)?(?:theorem|lemma|def|abbrev)\s+{re.escape(short)}\b", re.M)
    bp = cfg.root / cfg.blueprint_namespace
    if bp.is_dir():
        for p in sorted(bp.rglob("*.lean")):
            if pat.search(p.read_text(errors="replace")):
                return ".".join(p.relative_to(cfg.root).with_suffix("").parts)
    raise UsageError(f"no blueprint module declares {name}")


# ---------------------------------------------------------------------------
# rendering


def render_status_md(cfg: Config, nodes: list[Node], dests: list[Destination], problems: list[str], meta: dict) -> str:
    lines = ["# Frontier", "", f"Generated by `lockstep extract`; do not edit. Toolchain {meta.get('toolchain', '?')}; {meta.get('modules', '?')} modules in the environment.", ""]
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n.status] = counts.get(n.status, 0) + 1
    lines.append("Nodes: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "none") + ".")
    lines.append("")
    for ws in cfg.workstreams:
        group = [n for n in nodes if n.ws == ws]
        if not group:
            continue
        lines.append(f"## Workstream {ws}")
        lines.append("")
        lines.append("| node | kind | status | ready | blocked by | falsifier | opened |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for n in sorted(group, key=lambda x: x.name):
            f = (n.falsify or {}).get("result", "-") if n.falsifier else "-"
            lines.append(f"| `{n.name}` | {n.kind} | {n.status} | {'yes' if n.ready else ''} | {', '.join(n.blocked_by)} | {f} | {n.opened or ''} |")
        lines.append("")
    other = [n for n in nodes if n.ws not in cfg.workstreams]
    if other:
        lines.append("## Untagged")
        lines.append("")
        for n in other:
            lines.append(f"- `{n.name}` ({n.status}): {'; '.join(n.tag_errors)}")
        lines.append("")
    lines.append("## Destinations")
    lines.append("")
    lines.append("| theorem | module | sorry | non-standard axioms | uncovered |")
    lines.append("| --- | --- | --- | --- | --- |")
    for d in sorted(dests, key=lambda x: x.name):
        lines.append(f"| `{d.name}` | {d.module} | {'yes' if d.sorry else ''} | {', '.join(d.nonstandard_axioms)} | {', '.join(d.uncovered_axioms)} |")
    lines.append("")
    if problems:
        lines.append("## Problems")
        lines.append("")
        lines.extend(f"- {p}" for p in problems)
        lines.append("")
    return "\n".join(lines)


def opened_date(cfg: Config, module: str) -> str | None:
    from lockstep.gitutil import git

    rel = str(module_to_path(module))
    try:
        out = git(cfg, "log", "--diff-filter=A", "--follow", "--format=%as", "--", rel, check=False).strip().splitlines()
    except (RuntimeError, OSError):
        return None
    if out:
        return out[-1]
    return "uncommitted" if (cfg.root / rel).exists() else None


def opened_dates(cfg: Config, modules: list[str]) -> dict[str, str | None]:
    """Run the same read-only history queries with at most four Git processes."""
    from concurrent.futures import ThreadPoolExecutor

    names = list(dict.fromkeys(modules))
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(names))) as executor:
        dates = executor.map(lambda name: opened_date(cfg, name), names)
        return dict(zip(names, dates))


def days_open(opened: str | None) -> str:
    if not opened or opened == "uncommitted":
        return "new"
    try:
        t = time.mktime(time.strptime(opened, "%Y-%m-%d"))
    except ValueError:
        return "?"
    return str(int((time.time() - t) // 86400))
