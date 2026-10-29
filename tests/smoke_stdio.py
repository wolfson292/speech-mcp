#!/usr/bin/env python3
"""Smoke test the real MCP protocol over stdio.

    .venv/bin/python tests/smoke_stdio.py

Spawns server.py as a subprocess the way Claude Desktop and Claude Code do,
then over JSON-RPC: lists tools, checks every input schema is valid JSON
Schema, calls speech_info, transcribes the mono fixture, and confirms a path
outside SPEECH_MCP_ROOTS comes back as a tool error.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
EXPECTED = {"transcribe", "transcription_status", "diarize_only", "speech_info"}


async def main() -> None:
    transport = StdioTransport(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        env={**os.environ, "SPEECH_MCP_ROOTS": str(FIX)},
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert names == EXPECTED, names
        for t in tools:
            Draft202012Validator.check_schema(t.inputSchema)
            assert t.description, f"{t.name} has no description"
        schema = {t.name: t.inputSchema for t in tools}
        engines = schema["transcribe"]["properties"]["engine"]
        print("tools:", sorted(names))
        print("transcribe.engine schema:", json.dumps(engines))
        assert schema["transcribe"]["required"] == ["path"]

        info = json.loads((await client.call_tool("speech_info", {})).content[0].text)
        assert info["speech_binary"], info
        print("speech_info.speech_binary:", info["speech_binary"])
        print("speech_info.allowed_roots:", info["allowed_roots"])

        res = await client.call_tool("transcribe", {
            "path": str(FIX / "meeting_mono.wav"), "output": "text", "wait_seconds": 300,
        })
        text = res.content[0].text
        print("--- transcribe(meeting_mono.wav)\n" + text)
        assert "S01" in text and "S02" in text and "southeast" in text.lower()

        res = await client.call_tool("transcribe", {"path": "/etc/hosts"},
                                     raise_on_error=False)
        assert res.is_error, res
        print("--- transcribe(/etc/hosts) ->", res.content[0].text[:90])

        res = await client.call_tool("transcribe", {"path": "x", "engine": "rm -rf"},
                                     raise_on_error=False)
        assert res.is_error, res
        print("--- transcribe(engine='rm -rf') -> rejected by schema")
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
