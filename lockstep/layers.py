"""Layer checks: import direction, allowlists, blueprint root completeness."""

from __future__ import annotations

from dataclasses import dataclass

import os

from lockstep.config import Config
from lockstep.modules import Module, frozen_closure, input_layer_members, module_to_path, read_name_list, scan_modules


@dataclass
class Violation:
    kind: str
    module: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.module}: {self.detail}"


def check_layers(cfg: Config, mods: dict[str, Module] | None = None, allow_rebuild: bool | None = None, base: str | None = None) -> list[Violation]:
    layers = cfg.layers()
    mods = mods if mods is not None else scan_modules(cfg, layers)
    if allow_rebuild is None:
        allow_rebuild = os.environ.get("LOCKSTEP_CERTIFICATE_REBUILD") == "1"
    order = {layer.name: i for i, layer in enumerate(layers)}
    rules = {(r.from_layer, r.to_layer): r for r in cfg.layer_rules()}
    lists: dict[str, set[str]] = {}
    for r in cfg.layer_rules():
        if r.file:
            lists[r.file] = set(read_name_list(cfg.root / r.file))
    out: list[Violation] = []
    for m in sorted(mods.values(), key=lambda x: x.name):
        if m.layer is None:
            out.append(Violation("unclassified", m.name, "module belongs to no configured layer"))
            continue
        for imp in m.imports:
            target = mods.get(imp)
            if target is None or target.layer is None:
                continue  # external (mathlib etc.) or unclassified (reported once above)
            if target.layer == m.layer:
                continue
            rule = rules.get((m.layer, target.layer))
            if rule is None:
                allowed = order[m.layer] > order[target.layer]
                if not allowed:
                    out.append(Violation("layer-order", m.name, f"{m.layer} may not import {target.layer} ({imp})"))
                continue
            if rule.allow == "any":
                continue
            if rule.allow == "none":
                out.append(Violation("layer-rule", m.name, f"{m.layer} -> {target.layer} forbidden ({imp})"))
                continue
            allow = lists.get(rule.file or "", set())
            subject = m.name if rule.allow == "importers" else imp
            if subject not in allow:
                out.append(
                    Violation(
                        "layer-allowlist",
                        m.name,
                        f"imports {imp} ({target.layer}); {subject} is not listed in {rule.file}",
                    )
                )
    out.extend(check_blueprint_root(cfg, mods))
    out.extend(check_pinned_layers(cfg, mods, allow_rebuild, base))
    return out


def check_pinned_layers(cfg: Config, mods: dict[str, Module], allow_rebuild: bool, base: str | None = None) -> list[Violation]:
    """Closure layers with a members file: the computed set must equal the pinned list, and
    frozen members may not differ from the default branch without a rebuild marker."""
    out: list[Violation] = []
    for layer in cfg.layers():
        if not layer.inputs_of:
            continue
        if layer.members_file:
            pinned = set(read_name_list(cfg.root / layer.members_file))
            computed = input_layer_members(layer, mods)
            for name in sorted(computed - pinned):
                out.append(Violation("encodings-unpinned", name, f"is imported directly by {layer.inputs_of} modules but not listed in {layer.members_file}; add it deliberately (it becomes frozen)"))
            for name in sorted(pinned - computed):
                out.append(Violation("encodings-stale-pin", name, f"listed in {layer.members_file} but no {layer.inputs_of} module imports it any more"))
        if layer.frozen_closure and not allow_rebuild:
            from lockstep.gitutil import changed_files, git_ok

            if git_ok(cfg):
                closure = frozen_closure(layer, mods)
                ref = base or f"{cfg.git_remote}/{cfg.default_branch}"
                changed = set(changed_files(cfg, ref))
                by_path = {str(module_to_path(n)): n for n in closure}
                hit = sorted(n for rel, n in by_path.items() if rel in changed)
                if hit:
                    rev: dict[str, set[str]] = {}
                    for m in mods.values():
                        for i in m.imports:
                            rev.setdefault(i, set()).add(m.name)
                    for name in hit:
                        seen: set[str] = set()
                        stack = [name]
                        while stack:
                            x = stack.pop()
                            if x in seen:
                                continue
                            seen.add(x)
                            stack.extend(rev.get(x, ()))
                        n_gen = sum(1 for x in seen if x in mods and mods[x].generated)
                        out.append(Violation("frozen-module", name, f"{module_to_path(name)} differs from {ref} and {n_gen} generated certificate module(s) are built on it; they will all be rebuilt. Commit with the marker [certificate-rebuild] (or LOCKSTEP_CERTIFICATE_REBUILD=1) to accept that"))
    return out


def blueprint_node_modules(cfg: Config, mods: dict[str, Module]) -> list[str]:
    layer = cfg.layer(cfg.blueprint_layer)
    root_name = cfg.blueprint_namespace
    excluded = set(layer.exclude_from_root)
    return sorted(
        m.name
        for m in mods.values()
        if m.layer == layer.name and m.name != root_name and m.name not in excluded
    )


def check_blueprint_root(cfg: Config, mods: dict[str, Module]) -> list[Violation]:
    root_name = cfg.blueprint_namespace
    root = mods.get(root_name)
    if root is None:
        return [Violation("blueprint-root", root_name, f"{cfg.blueprint_root} is missing")]
    imported = set(root.imports)
    out = []
    for name in blueprint_node_modules(cfg, mods):
        if name not in imported:
            out.append(Violation("blueprint-root", name, f"not imported by {root_name}"))
    return out


def derive_allowlists(cfg: Config, mods: dict[str, Module] | None = None) -> dict[str, list[str]]:
    """For every allowlist rule and pinned closure layer, the names the current import graph would require."""
    mods = mods if mods is not None else scan_modules(cfg)
    result: dict[str, list[str]] = {}
    for layer in cfg.layers():
        if layer.inputs_of and layer.members_file:
            result[layer.members_file] = sorted(input_layer_members(layer, mods))
    for r in cfg.layer_rules():
        if r.allow not in ("importers", "imported") or not r.file:
            continue
        names: set[str] = set()
        for m in mods.values():
            if m.layer != r.from_layer:
                continue
            for imp in m.imports:
                t = mods.get(imp)
                if t is not None and t.layer == r.to_layer:
                    names.add(m.name if r.allow == "importers" else imp)
        result[r.file] = sorted(set(result.get(r.file, [])) | names)
    return result
