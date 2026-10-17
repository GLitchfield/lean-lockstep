"""Declarative command specs shared by the CLI (argparse) and the MCP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Arg:
    name: str  # python identifier; CLI flag is --name-with-dashes unless positional
    help: str
    type: str = "str"  # str | int | float | bool | list
    positional: bool = False
    required: bool = False
    default: Any = None
    choices: list[str] | None = None


@dataclass
class CommandSpec:
    name: str  # MCP tool name, e.g. "node_show"
    cli: list[str]  # CLI path, e.g. ["node", "show"]
    help: str
    run: Callable[..., Any]
    args: list[Arg] = field(default_factory=list)
    long_running: bool = False  # MCP returns a job id
    mcp: bool = True


REGISTRY: list[CommandSpec] = []


def command(spec: CommandSpec) -> CommandSpec:
    REGISTRY.append(spec)
    return spec
