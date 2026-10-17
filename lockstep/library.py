"""Deterministic discovery of curated research code and ``lockstep`` workflows.

The catalog is data, not a plugin mechanism.  Source validation parses Python
files with :mod:`ast`; it never imports or executes catalogued modules.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from lockstep.errors import UsageError


CATALOG_REL = Path("harness/research/catalog.json")
KINDS = ("primitive", "workflow")
_REQUIRED_TEXT = ("id", "kind", "title", "summary", "api", "example", "semantics")


def _fail(message: str) -> None:
    raise UsageError(f"invalid library catalog: {message}")


def _symbols(path: Path) -> set[str]:
    """Return statically declared module and class symbols from a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        _fail(f"cannot parse source {path}: {exc}")
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    found.add(f"{node.name}.{member.name}")
    return found


def _source(root: Path, ident: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{ident}: source must be an object")
    rel = value.get("path")
    symbols = value.get("symbols")
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        _fail(f"{ident}: source.path must be a nonempty project-relative path")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(x, str) and x for x in symbols):
        _fail(f"{ident}: source.symbols must be a nonempty string list")
    if len(set(symbols)) != len(symbols):
        _fail(f"{ident}: source.symbols contains duplicates")
    project = root.resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(project) or not path.is_file():
        _fail(f"{ident}: source file is missing or outside the project: {rel}")
    if path.suffix != ".py":
        _fail(f"{ident}: symbol validation currently requires a Python source: {rel}")
    missing = sorted(set(symbols) - _symbols(path))
    if missing:
        _fail(f"{ident}: stale source {rel}; missing symbol(s): {', '.join(missing)}")
    return {"path": rel, "symbols": list(symbols)}


def load(root: Path) -> list[dict[str, Any]]:
    """Load and fully validate the project's curated catalog."""
    path = root / CATALOG_REL
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"cannot read {CATALOG_REL}: {exc}")
    except json.JSONDecodeError as exc:
        _fail(f"cannot parse {CATALOG_REL}: {exc}")
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        _fail("schema_version must be 1")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        _fail("entries must be a list")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            _fail(f"entry {index} must be an object")
        for key in _REQUIRED_TEXT:
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                _fail(f"entry {index}: {key} must be a nonempty string")
        ident = raw["id"]
        if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", ident):
            _fail(f"entry {index}: invalid id {ident!r}")
        if ident in seen:
            _fail(f"duplicate entry id {ident}")
        seen.add(ident)
        if raw["kind"] not in KINDS:
            _fail(f"{ident}: kind must be one of {', '.join(KINDS)}")
        if raw["kind"] == "workflow" and not raw["api"].lstrip().startswith("lockstep "):
            _fail(f"{ident}: workflow api must name the public `lockstep` command surface")
        keywords = raw.get("keywords")
        if not isinstance(keywords, list) or not keywords or not all(isinstance(x, str) and x.strip() for x in keywords):
            _fail(f"{ident}: keywords must be a nonempty string list")
        entry = {key: raw[key].strip() for key in _REQUIRED_TEXT}
        entry["keywords"] = [word.strip() for word in keywords]
        entry["source"] = _source(root, ident, raw.get("source"))
        entries.append(entry)
    return entries


def brief(entry: dict[str, Any], *, score: int | None = None) -> dict[str, Any]:
    result = {key: entry[key] for key in ("id", "kind", "title", "summary")}
    if score is not None:
        result["score"] = score
    return result


def listing(root: Path, *, kind: str | None = None) -> list[dict[str, Any]]:
    if kind is not None and kind not in KINDS:
        raise UsageError(f"kind must be one of {', '.join(KINDS)}")
    return [brief(entry) for entry in load(root) if kind is None or entry["kind"] == kind]


def show(root: Path, ident: str) -> dict[str, Any]:
    for entry in load(root):
        if entry["id"] == ident:
            return entry
    raise UsageError(f"unknown library entry {ident!r}; run `lockstep lib list`")


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def _forms(token: str) -> set[str]:
    forms = {token}
    if len(token) > 3 and token.endswith("s"):
        forms.add(token[:-1])
    return forms


def _contains(forms: set[str], tokens: set[str]) -> bool:
    return bool(forms & tokens)


def _index_tokens(value: str) -> set[str]:
    return {form for token in _tokens(value) for form in _forms(token)}


def _score(entry: dict[str, Any], query: str) -> int:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0
    identity = _index_tokens(entry["id"] + " " + entry["title"])
    keywords = _index_tokens(" ".join(entry["keywords"]))
    summary = _index_tokens(entry["summary"])
    detail = _index_tokens(entry["api"] + " " + entry["semantics"])
    score = matched = 0
    for token in query_tokens:
        forms = _forms(token)
        weight = 0
        if _contains(forms, identity):
            weight = 12
        elif _contains(forms, keywords):
            weight = 8
        elif _contains(forms, summary):
            weight = 4
        elif _contains(forms, detail):
            weight = 1
        if weight:
            matched += 1
            score += weight
    normalized_query = " ".join(query_tokens)
    normalized_entry = " ".join(_tokens(entry["title"] + " " + " ".join(entry["keywords"])))
    if normalized_query in normalized_entry:
        score += 10
    # Prefer entries answering the whole task while still returning useful
    # partial matches for short exploratory queries.
    return score + (6 * matched // len(query_tokens)) if matched else 0


def search(root: Path, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    if not query or not _tokens(query):
        raise UsageError("search query must contain a word or number")
    if not 1 <= limit <= 20:
        raise UsageError("limit must be between 1 and 20")
    ranked = [(score, entry) for entry in load(root) if (score := _score(entry, query))]
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [brief(entry, score=score) for score, entry in ranked[:limit]]
