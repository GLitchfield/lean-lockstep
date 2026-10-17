"""Docstring tag parser: ``@tag value`` lines after the natural-language description."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TAG_RE = re.compile(r"^\s*@([a-z][a-z0-9-]*)(?:\s+(.*?))?\s*$")

KINDS = ("formalization", "conjecture", "refutation")
KNOWN_TAGS = ("node", "ws", "kind", "depends", "consumer", "dangling-ok", "falsifier", "measure", "target")
LIST_TAGS = ("depends",)


@dataclass
class Tags:
    description: str = ""
    tags: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def is_node(self) -> bool:
        return "node" in self.tags

    def get(self, name: str, default=None):
        return self.tags.get(name, default)


def parse_docstring(doc: str | None) -> Tags:
    out = Tags()
    if not doc:
        return out
    desc: list[str] = []
    for raw in doc.splitlines():
        m = TAG_RE.match(raw)
        if m:
            tag, value = m.group(1), (m.group(2) or "").strip()
            if tag in LIST_TAGS:
                out.tags[tag] = [v.strip() for v in value.split(",") if v.strip()]
            elif tag in out.tags:
                out.errors.append(f"duplicate tag @{tag}")
            else:
                out.tags[tag] = value
        else:
            desc.append(raw)
    out.description = "\n".join(desc).strip()
    return out


def validate_tags(t: Tags, workstreams: list[str]) -> list[str]:
    errs = list(t.errors)
    if not t.is_node:
        return errs
    for req in ("ws", "kind"):
        if not t.get(req):
            errs.append(f"missing required tag @{req}")
    ws = t.get("ws")
    if ws and ws not in workstreams:
        errs.append(f"unknown workstream @ws {ws}")
    kind = t.get("kind")
    if kind and kind not in KINDS:
        errs.append(f"unknown @kind {kind} (expected one of {', '.join(KINDS)})")
    if kind == "conjecture" and not t.get("falsifier"):
        errs.append("conjecture nodes need @falsifier")
    if not t.get("consumer") and "dangling-ok" not in t.tags:
        errs.append("missing @consumer (or @dangling-ok <reason>)")
    if "dangling-ok" in t.tags and not t.get("dangling-ok"):
        errs.append("@dangling-ok needs a reason")
    for tag in t.tags:
        if tag not in KNOWN_TAGS:
            errs.append(f"unknown tag @{tag}")
    return errs


def render_docstring(description: str, tags: dict[str, object]) -> str:
    lines = [description.strip(), "@node"]
    for key in ("ws", "kind", "depends", "consumer", "dangling-ok", "falsifier", "measure", "target"):
        if key not in tags or tags[key] in (None, "", []):
            continue
        val = tags[key]
        if isinstance(val, list):
            val = ", ".join(val)
        lines.append(f"@{key} {val}".rstrip())
    return "\n".join(lines)
