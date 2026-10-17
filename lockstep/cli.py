"""``lockstep`` command-line entry point: argparse built from the shared command specs."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from lockstep import EXIT_USAGE, __version__
from lockstep.errors import LockstepError
from lockstep.output import Result, emit
from lockstep.spec import REGISTRY, Arg, CommandSpec


def _add_arg(parser: argparse.ArgumentParser, a: Arg) -> None:
    kwargs: dict = {"help": a.help}
    if a.choices:
        kwargs["choices"] = a.choices
    if a.type == "int":
        kwargs["type"] = int
    elif a.type == "float":
        kwargs["type"] = float
    if a.positional:
        if not a.required:
            kwargs["nargs"] = "?"
            kwargs["default"] = a.default
        if a.type == "list":
            kwargs["nargs"] = "+" if a.required else "*"
        parser.add_argument(a.name, **kwargs)
        return
    flag = "--" + a.name.rstrip("_").replace("_", "-")
    if a.type == "bool":
        parser.add_argument(flag, dest=a.name, action="store_true", default=bool(a.default), help=a.help)
        return
    if a.type == "list":
        kwargs["action"] = "append"
        kwargs["default"] = None
    else:
        kwargs["default"] = a.default
    kwargs["required"] = a.required
    kwargs["dest"] = a.name
    parser.add_argument(flag, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    import lockstep.commands  # noqa: F401  (registers commands)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON instead of tables")
    common.add_argument("--root", default=argparse.SUPPRESS, help="project root (default: discover harness/config.toml upwards)")
    common.add_argument("--full", action="store_true", default=argparse.SUPPRESS, help="explicitly fetch full output instead of an artifact reference")
    parser = argparse.ArgumentParser(prog="lockstep", description="Blueprint harness: state derived from Lean.", parents=[common])
    parser.add_argument("--version", action="version", version=f"lockstep {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    groups: dict[str, argparse._SubParsersAction] = {}
    for spec in REGISTRY:
        if len(spec.cli) == 1:
            p = sub.add_parser(spec.cli[0], help=spec.help, description=spec.help, parents=[common])
        else:
            head = spec.cli[0]
            if head not in groups:
                gp = sub.add_parser(head, help=f"{head} commands")
                groups[head] = gp.add_subparsers(dest=f"{head}_cmd")
            p = groups[head].add_parser(spec.cli[1], help=spec.help, description=spec.help, parents=[common])
        p.set_defaults(_spec=spec)
        for a in spec.args:
            _add_arg(p, a)
    return parser


def dispatch(spec: CommandSpec, ns: argparse.Namespace) -> Result:
    from lockstep.context import Ctx

    root = Path(ns.root).resolve() if getattr(ns,'root',None) else None
    ctx = Ctx.load(root, json_mode=getattr(ns,'json',False))
    ctx.full_output = bool(getattr(ns,'full',False))
    kwargs = {}
    for a in spec.args:
        val = getattr(ns, a.name, a.default)
        if a.type == "list" and val is not None and not a.positional:
            # allow both repeated flags and comma-separated values
            flat: list[str] = []
            for v in val:
                flat.extend(x.strip() for x in str(v).split(",") if x.strip())
            val = flat
        if val is None and a.default is None and not a.positional:
            continue  # let the handler's own default apply
        kwargs[a.name] = val
    return spec.run(ctx, **kwargs)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["hook"]:  # hooks run before every agent shell command: skip the command registry
        from lockstep.hooks import main as hook_main

        return hook_main(args[1:])
    parser = build_parser()
    ns = parser.parse_args(argv)
    for key, default in (('json',False),('root',None),('full',False)):
        if not hasattr(ns,key): setattr(ns,key,default)
    spec = getattr(ns, "_spec", None)
    if spec is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        result = dispatch(spec, ns)
    except LockstepError as exc:
        res = Result(exit_code=exc.exit_code, payload={"error": str(exc)}, human=[f"error: {exc}"])
        emit(res, ns.json, sys.stderr if not ns.json else sys.stdout)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - last resort
        traceback.print_exc()
        res = Result(exit_code=EXIT_USAGE, payload={"error": f"{type(exc).__name__}: {exc}"}, human=[f"error: {exc}"])
        emit(res, ns.json, sys.stderr if not ns.json else sys.stdout)
        return EXIT_USAGE
    original_output = {'payload':result.payload,'text':'\n'.join(result.human)}
    if not ns.full and spec.name != 'artifact_show':
        from lockstep.artifacts import compact, put_json
        from lockstep.config import load_config
        import json
        cfg = load_config(Path(ns.root).resolve() if ns.root else None)
        limit = int(cfg.get('automation','output_bytes',default=4096))
        if ns.json and isinstance(result.payload,dict):
            result.payload = compact(cfg,result.payload,limit)
        elif len('\n'.join(result.human).encode()) > limit:
            from lockstep.errors import ReadOnly
            try:
                ref = put_json(cfg,{'payload':result.payload,'text':'\n'.join(result.human)},'command-detail-v1')
                result.human = [f'Full {spec.name} output saved ({ref["bytes"]} bytes).',f'lockstep artifact show {ref["id"]} --limit 4096  (or repeat with --full)']
            except (OSError,ReadOnly):
                pass  # Preserve read commands when no artifact can be written.
    try:
        from lockstep.metrics import record
        from lockstep.context import Ctx
        context=Ctx.load(Path(ns.root).resolve() if ns.root else None)
        record(context.cfg,context.agent,spec.name,original_output,result.payload if ns.json else {'text':'\n'.join(result.human)})
    except (LockstepError,OSError,ValueError,TypeError):
        pass  # Delayed/missing metering never changes a completed command's outcome.
    emit(result, ns.json)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
