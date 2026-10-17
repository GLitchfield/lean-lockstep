from __future__ import annotations

import json

from lockstep import EXIT_OK
from lockstep.context import Ctx
from lockstep.output import Result, table
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def run(ctx: Ctx, query: str | None = None, type_hash: str | None = None, limit: int = 20, ns: str | None = None) -> Result:
    cfg = ctx.cfg
    res = Result()
    hits: list[dict] = []
    if type_hash:
        for path in (cfg.index_json, cfg.nodes_json):
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            recs = data if isinstance(data, list) else data.get("nodes", []) + data.get("destinations", [])
            hits.extend({"name": r["name"], "module": r["module"], "kind": r.get("kind", "node"), "doc": r.get("doc") or r.get("description", ""), "type_pp": r["type_pp"], "score": 0} for r in recs if r.get("type_hash") == type_hash)
    elif query:
        with Store(cfg) as st:
            hits = st.search(query, limit=limit, namespace=ns)
    rows = [[h["name"], h["module"], f"{h.get('score', 0):.1f}", ((h.get("doc") or "").strip().splitlines() or [""])[0][:80]] for h in hits]
    if not rows:
        res.line("no results")
    res.human.extend(table(rows, ["name", "module", "score", "docstring"]))
    res.payload = {"hits": hits}
    res.exit_code = EXIT_OK
    return res


command(CommandSpec(name="search", cli=["search"], help="Full-text search over names, docstrings, and statements of core declarations and nodes.", run=run,
                    args=[Arg("query", "search terms (FTS5 syntax allowed)", positional=True), Arg("type_hash", "exact statement hash lookup"),
                          Arg("limit", "max results", "int", default=20), Arg("ns", "restrict to a name prefix")]))
