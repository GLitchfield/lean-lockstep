"""Commands for verified, immutable sharing of compiled certificate artifacts."""

from __future__ import annotations

from pathlib import Path
import time

from lockstep.artifact_cache import DEFAULT_FACETS, default_cache_root, share_certificates
from lockstep.context import Ctx
from lockstep.output import Result
from lockstep.spec import Arg, CommandSpec, command


def run_share(
    ctx: Ctx,
    other: str,
    apply: bool = False,
    cache: str | None = None,
    receipt: str | None = None,
    facet: list[str] | None = None,
) -> Result:
    cache_root = Path(cache).expanduser() if cache else default_cache_root(ctx.cfg)
    if not cache_root.is_absolute():
        cache_root = ctx.cfg.root / cache_root
    if receipt:
        receipt_path = Path(receipt).expanduser()
        if not receipt_path.is_absolute():
            receipt_path = ctx.cfg.root / receipt_path
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        receipt_path = ctx.cfg.state_home / "storage-receipts" / f"share-{stamp}.jsonl"
    payload = share_certificates(
        ctx.cfg, Path(other).expanduser().resolve(), cache_root=cache_root, receipt_path=receipt_path,
        facets=facet or DEFAULT_FACETS, apply=apply,
    )
    result = Result(payload=payload)
    result.line(
        f"{payload['mode']}: verified {payload['verified']} exact certificate artifacts "
        f"({payload['candidate_bytes'] / 2**30:.3f} GiB candidates), "
        f"adopted {payload['adopted']} ({payload['deduplicated_logical_bytes'] / 2**30:.3f} GiB logical duplicates); "
        f"{payload['eligible_freed_allocated_bytes'] / 2**30:.3f} GiB of replaced single-link blocks were eligible to free"
    )
    result.line(f"receipt: {payload['receipt']}")
    return result


command(CommandSpec(
    name="storage_share", cli=["storage", "share"],
    help="Verify and hard-link byte-identical compiled certificate artifacts through Lake's native immutable cache.",
    run=run_share, long_running=True,
    args=[
        Arg("other", "other compatible checkout", required=True),
        Arg("apply", "atomically adopt verified native cache blobs (default is verification-only dry run)", "bool"),
        Arg("cache", "shared Lake cache directory (default is project/toolchain-specific harness state)"),
        Arg("receipt", "JSONL receipt to create or resume"),
        Arg("facet", "compiled facet(s); defaults to every supported module output", "list"),
    ],
))
