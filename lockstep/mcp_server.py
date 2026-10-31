"""MCP server exposing every `lockstep` command as a tool with the same name and arguments.

Run with ``python -m lockstep.mcp_server`` (stdio).  Long-running commands (build, check,
falsify, node add/restate/retire, close, extract) run synchronously unless the
caller passes ``async_=true`` where supported; ``job_status``/``job_log`` poll.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
from contextlib import redirect_stdout
from typing import Any, Optional

from lockstep.context import Ctx
from lockstep.errors import LockstepError
from lockstep.spec import REGISTRY, Arg, CommandSpec

_PY_TYPES = {"str": str, "int": int, "float": float, "bool": bool, "list": list[str]}


def _make_tool(spec: CommandSpec):
    params = []
    for a in spec.args:
        py = _PY_TYPES.get(a.type, str)
        if a.required:
            params.append(inspect.Parameter(a.name, inspect.Parameter.KEYWORD_ONLY, annotation=py))
        else:
            default = a.default if a.default is not None else (False if a.type == "bool" else None)
            ann = py if a.type == "bool" else Optional[py]
            params.append(inspect.Parameter(a.name, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=default))

    def tool(**kwargs: Any) -> dict:
        ctx = Ctx.load(json_mode=True)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                result = spec.run(ctx, **{k: v for k, v in kwargs.items() if v is not None})
        except LockstepError as exc:
            return {"ok": False, "exit_code": exc.exit_code, "error": str(exc)}
        payload = result.payload if isinstance(result.payload, dict) else {"result": result.payload}
        from lockstep.artifacts import compact
        envelope = {"ok": result.exit_code == 0, "exit_code": result.exit_code, **payload}
        # Keep human tables in CLI output; repeating them in MCP doubles retrieval cost.
        if spec.name == 'artifact_show':
            return envelope
        returned = compact(ctx.cfg, envelope, int(ctx.cfg.get('automation', 'output_bytes', default=4096)))
        from lockstep.metrics import record
        try:
            record(ctx.cfg,ctx.agent,spec.name,{'payload':payload,'text':'\n'.join(result.human)},returned)
        except (LockstepError,OSError,ValueError,TypeError):
            pass
        return returned

    tool.__name__ = spec.name
    tool.__doc__ = spec.help + "\n\n" + "\n".join(f"{a.name}: {a.help}" for a in spec.args)
    tool.__signature__ = inspect.Signature(params, return_annotation=dict)  # type: ignore[attr-defined]
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    return tool


def _mcp_tool(spec: CommandSpec):
    """Wrap ``_make_tool`` for the MCP SDK.

    Two things the plain dict tool cannot say on the wire: a failed command (``ok`` false) is
    returned with ``isError: true``, so clients and gateways that count failures see it, and each
    argument's help text becomes the ``description`` of its property in the input schema, where
    the model reads it.  ``_make_tool`` stays free of MCP and pydantic imports for the core tests.
    """
    from typing import Annotated

    from mcp.types import CallToolResult
    from pydantic import Field

    inner = _make_tool(spec)
    helps = {a.name: a.help for a in spec.args if a.help}

    def tool(**kwargs: Any) -> CallToolResult:
        envelope = inner(**kwargs)
        text = json.dumps(envelope, indent=2, ensure_ascii=False, default=str)
        failed = isinstance(envelope, dict) and envelope.get("ok") is False
        return CallToolResult.model_validate({"content": [{"type": "text", "text": text}], "isError": failed})

    sig = inspect.signature(inner)
    params = [
        p.replace(annotation=Annotated[p.annotation, Field(description=helps[p.name])]) if p.name in helps else p
        for p in sig.parameters.values()
    ]
    tool.__name__ = spec.name
    tool.__doc__ = spec.help
    tool.__signature__ = sig.replace(parameters=params, return_annotation=CallToolResult)  # type: ignore[attr-defined]
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": CallToolResult}
    return tool


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2
    except ImportError:  # pragma: no cover
        from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x
    import lockstep.commands  # noqa: F401

    server = MCPServer("lockstep")
    for spec in REGISTRY:
        if not spec.mcp:
            continue
        server.tool(name=spec.name, description=spec.help)(_mcp_tool(spec))
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
