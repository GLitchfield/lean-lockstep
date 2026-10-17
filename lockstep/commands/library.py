"""Compact CLI/MCP views of the curated research library catalog."""

from __future__ import annotations

from lockstep import library
from lockstep.output import Result, table
from lockstep.spec import Arg, CommandSpec, command


def _summary_result(entries: list[dict], *, query: str | None = None) -> Result:
    fields = ["id", "kind", "summary"]
    if query is not None:
        fields.insert(2, "score")
    rows = [[entry.get(field, "") for field in fields] for entry in entries]
    human = table(rows, fields)
    if not entries:
        human = [f"no library entries match {query!r}" if query is not None else "library catalog is empty"]
    return Result(0, {"entries": entries}, human)


def listing(ctx, kind=None):
    return _summary_result(library.listing(ctx.cfg.root, kind=kind))


def search(ctx, query, limit=5):
    return _summary_result(library.search(ctx.cfg.root, query, limit=limit), query=query)


def show(ctx, id):
    entry = library.show(ctx.cfg.root, id)
    source = entry["source"]
    human = [
        f"{entry['id']}  [{entry['kind']}]  {entry['title']}",
        entry["summary"],
        f"API: {entry['api']}",
        "Example:",
        entry["example"],
        f"Semantics: {entry['semantics']}",
        f"Source: {source['path']} :: {', '.join(source['symbols'])}",
    ]
    return Result(0, {"entry": entry}, human)


command(CommandSpec(
    "lib_list",
    ["lib", "list"],
    "List compact summaries of validated reusable research code and workflows.",
    listing,
    [Arg("kind", "restrict to primitive or workflow", choices=list(library.KINDS))],
))
command(CommandSpec(
    "lib_search",
    ["lib", "search"],
    "Search the validated research catalog; use lib_show to inspect one result.",
    search,
    [
        Arg("query", "plain task terms", positional=True, required=True),
        Arg("limit", "maximum compact results (1..20)", "int", default=5),
    ],
))
command(CommandSpec(
    "lib_show",
    ["lib", "show"],
    "Show one catalogued API, example, semantics and source reference.",
    show,
    [Arg("id", "catalog entry ID", positional=True, required=True)],
))
