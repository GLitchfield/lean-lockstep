"""What MCP clients see on the wire: argument help in the input schema, and isError on failure."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")


def _server():
    from lockstep.mcp_server import build_server

    return build_server()


def test_input_schema_carries_argument_help():
    from lockstep.spec import REGISTRY

    tools = {t.name: t for t in asyncio.run(_server().list_tools())}
    spec = next(s for s in REGISTRY if s.name == "claim")
    props = tools["claim"].input_schema["properties"]
    for arg in spec.args:
        assert props[arg.name]["description"] == arg.help
    # The tool description stays the command help; argument help is not repeated there.
    assert tools["claim"].description == spec.help


def test_every_documented_argument_has_a_schema_description():
    tools = asyncio.run(_server().list_tools())
    missing = [
        (t.name, name)
        for t in tools
        for name, prop in t.input_schema.get("properties", {}).items()
        if "description" not in prop
    ]
    assert missing == []


def test_failed_command_is_reported_with_is_error(cfg):
    result = asyncio.run(_server().call_tool("node_show", {"name": "no_such_node"}))
    assert result.is_error is True
    envelope = json.loads(result.content[0].text)
    assert envelope["ok"] is False and envelope["exit_code"] != 0 and "error" in envelope


def test_successful_command_is_not_an_error(cfg):
    result = asyncio.run(_server().call_tool("job_list", {}))
    assert result.is_error is False
    assert json.loads(result.content[0].text)["ok"] is True
