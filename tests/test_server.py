"""The server through the SDK's client: in-process on recorded responses, and once as a real stdio subprocess."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from pse_opendata_mcp import __version__
from pse_opendata_mcp.server import build_server

from conftest import Recorded

TOOLS = ["list_reports", "describe_report", "get_report", "prices", "generation", "demand", "cross_border_flows",
         "res_curtailment", "summarize"]

CALLS: list[tuple[str, dict[str, Any], str]] = [
    ("list_reports", {}, "rce-pln,15min,2024-06-14,PLN/MWh"),
    ("describe_report", {"report": "poze-redoze"}, "wi_red_network,MW"),
    ("get_report", {"report": "kse-load", "start": "2026-10-24", "end": "2026-10-26"}, "day,load_fcst,load_actual"),
    ("prices", {"start": "2026-10-24", "end": "2026-10-26", "kind": "both", "resolution": "day"}, "day,rce_mean"),
    ("generation", {"start": "2026-10-25", "resolution": "day"}, "day,wi,pv"),
    ("demand", {"start": "2026-10-25", "forecast": True, "resolution": "day"}, "day,actual_mean"),
    ("cross_border_flows", {"start": "2026-10-25", "neighbours": ["DE", "CZ"]}, "t,CZ,DE,total"),
    ("res_curtailment", {"start": "2026-10-24", "end": "2026-10-26"}, "day,pv_bal,pv_net,wi_bal,wi_net,total,h"),
    ("summarize", {"report": "rce-pln", "start": "2026-10-25"}, "field,n,miss,mean,min,max,t_min,t_max,le0_h,neg_h"),
]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def text_of(result: Any) -> str:
    assert len(result.content) == 1 and result.content[0].type == "text"
    return result.content[0].text


def test_server_lists_the_tools(rec: Recorded) -> None:
    async def main() -> Any:
        async with Client(build_server(rec.source)) as client:
            return await client.list_tools()

    tools = run(main()).tools
    assert [t.name for t in tools] == TOOLS
    assert all(t.description and t.output_schema is None for t in tools)
    for tool in tools:
        for name, prop in tool.input_schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"
    # the tool list is sent to the model in every session: keep it small
    size = len(json.dumps([t.model_dump(exclude_none=True) for t in tools]))
    assert size < 12000, size


def test_every_tool_answers_through_the_client(rec: Recorded) -> None:
    async def main() -> list[Any]:
        async with Client(build_server(rec.source)) as client:
            return [await client.call_tool(name, args) for name, args, _ in CALLS]

    assert [name for name, _, _ in CALLS] == TOOLS
    for (name, _, expected), result in zip(CALLS, run(main()), strict=True):
        assert not result.is_error, f"{name}: {text_of(result)}"
        assert result.structured_content is None          # one copy of the answer, not two
        text = text_of(result)
        assert expected in text, f"{name}: {text}"
        assert text.startswith(("source: PSE S.A.", "report: ")), name
        if name not in ("list_reports", "describe_report"):
            assert "retrieved 2026-11-16T12:00Z" in text, name


def test_errors_reach_the_model_as_readable_tool_errors(rec: Recorded) -> None:
    async def main() -> list[Any]:
        async with Client(build_server(rec.source)) as client:
            return [
                await client.call_tool("prices", {"start": "2025-01-01", "end": "2026-10-26", "resolution": "hour"}),
                await client.call_tool("get_report", {"report": "przeplywy-mocy", "start": "2026-10-25"}),
                await client.call_tool("prices", {"start": "2026-10-25", "resolution": "week"}),
                await client.call_tool("prices", {}),
            ]

    too_long, directional, bad_enum, no_start = run(main())
    assert all(r.is_error for r in (too_long, directional, bad_enum, no_start))
    assert "the limit for resolution='hour' is 31 days. Narrow the range or use resolution='month'" in text_of(too_long)
    assert "use cross_border_flows" in text_of(directional)
    assert "resolution" in text_of(bad_enum) and "start" in text_of(no_start)
    assert rec.session.calls == []


def test_republication_check_parameter_reaches_the_library(rec: Recorded) -> None:
    async def main() -> Any:
        async with Client(build_server(rec.source)) as client:
            return await client.call_tool("demand", {"start": "2026-10-25", "check_republished": True})

    assert "republication check on" in text_of(run(main()))
    assert rec.checks == [True]


def test_stdio_subprocess_serves_the_catalogue() -> None:
    """The installed entry point over real stdio; the catalogue tools need no network."""
    async def main() -> tuple[Any, Any, Any]:
        params = StdioServerParameters(command=sys.executable, args=["-m", "pse_opendata_mcp", "--no-cache"])
        async with Client(params) as client:
            tools = await client.list_tools()
            return tools, await client.call_tool("list_reports", {}), await client.call_tool(
                "describe_report", {"report": "rce-pln"})

    tools, listing, described = run(main())
    assert [t.name for t in tools.tools] == TOOLS
    assert "przeplywy-mocy,15min" in text_of(listing) and "rce_pln,PLN/MWh" in text_of(described)


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from pse_opendata_mcp.server import main

    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0 and capsys.readouterr().out.strip() == f"pse-opendata-mcp {__version__}"
