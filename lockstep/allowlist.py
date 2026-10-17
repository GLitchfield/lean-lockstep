"""Axiom allowlist: which non-standard axioms may reach which declarations."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path


def read_allowlist(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text() or "[]")
    return data if isinstance(data, list) else []


def write_allowlist(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")


def uncovered(entries: list[dict], name: str, axioms: list[str], standard: list[str]) -> list[str]:
    """Axioms of ``name`` (beyond the standard set) not covered by any allowlist entry."""
    out = []
    for ax in axioms:
        if ax in standard:
            continue
        ok = False
        for e in entries:
            pat = str(e.get("axiom", ""))
            # Lean 4.29 gives every `native_decide` its own auxiliary axiom
            # (`<decl>._native.native_decide.ax_1_1`), so entries may be globs.
            if pat != ax and not fnmatch.fnmatchcase(ax, pat):
                continue
            reaches = e.get("reaches", [])
            if "*" in reaches or name in reaches:
                ok = True
                break
        if not ok:
            out.append(ax)
    return out
