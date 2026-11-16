"""The MCP server: tools over :mod:`pse_opendata_mcp.core`.

Usage:
    pse-opendata-mcp                                  # stdio, for an MCP client
    pse-opendata-mcp --cache-dir data/pse             # choose the month cache directory
    pse-opendata-mcp --transport streamable-http      # HTTP on 127.0.0.1:8000/mcp
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
from collections.abc import Callable
from typing import Annotated, Any, Literal

import pse_opendata as pse
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from pse_opendata_mcp import __version__, core

INSTRUCTIONS = (
    "Polish power-system data from the public reports of PSE S.A. (the transmission system operator), from "
    "2024-06-14. Dates are Europe/Warsaw business days, YYYY-MM-DD, inclusive. Answers are compact text: header "
    "lines (source report, retrieval time, period, time zone, columns with units) and one CSV table; an empty cell "
    "means no data, never zero. Ask for aggregates first (resolution day or month, or summarize); raw rows are "
    "capped at max_rows. When you quote numbers, name PSE S.A. as the source and the retrieval date."
)

Start = Annotated[str, Field(description="First business day, YYYY-MM-DD")]
End = Annotated[str | None, Field(description="Last business day, inclusive; default: start")]
Resolution = Annotated[
    Literal["auto", "native", "hour", "day", "month"],
    Field(description="Row size. auto: hour up to 2 days, day up to 92, else month. Range limits in days: "
                      "native 7, hour 31, day 366, month 1100"),
]
MaxRows = Annotated[int, Field(description="Row cap, 1-500; longer tables are truncated with a note")]
Check = Annotated[bool, Field(description="Ask PSE whether cached months were republished later and reload them")]


def _guard(fn: Callable[..., str]) -> Callable[..., str]:
    """Report anticipated failures as tool errors the model can read and correct."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except (core.InputError, core.DataError) as e:
            raise ToolError(str(e)) from e
    return wrapper


def build_server(source: core.Source | None = None) -> MCPServer:
    """The server with all tools; ``source`` replaces the default PSE client (tests use recorded responses)."""
    src = source or core.Source()
    server = MCPServer("pse-opendata", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")

    def tool(fn: Callable[..., str]) -> Callable[..., str]:
        # plain text only: a structured copy would send every answer twice
        return server.tool(structured_output=False)(_guard(fn))

    @tool
    def list_reports() -> str:
        """List the PSE reports this server can read: name, interval, first date, units, title."""
        return core.list_reports()

    @tool
    def describe_report(report: Annotated[str, Field(description="Report name, e.g. rce-pln")]) -> str:
        """Fields, units, row keys and known caveats of one report."""
        return core.describe_report(report)

    @tool
    def get_report(
        report: Annotated[str, Field(description="Report name from list_reports")],
        start: Start,
        end: End = None,
        fields: Annotated[list[str] | None, Field(description="Fields to return; default: all")] = None,
        resolution: Resolution = "auto",
        agg: Annotated[Literal["mean", "sum", "min", "max"] | None,
                       Field(description="Statistic per row; default: sum for MWh fields, else mean")] = None,
        max_rows: MaxRows = 200,
        check_republished: Check = False,
    ) -> str:
        """Any catalogued report as a time series (balancing prices and volumes, capacity, plans, unit output).
        Prefer the dedicated tools for prices, demand, generation, flows and curtailment."""
        return core.get_report(src, report, start, end, fields, resolution, agg, max_rows, check_republished)

    @tool
    def prices(
        start: Start,
        end: End = None,
        kind: Annotated[Literal["rce", "sdac", "both"],
                        Field(description="rce: RCE market price; sdac: day-ahead coupling price")] = "rce",
        resolution: Resolution = "auto",
        max_rows: MaxRows = 200,
        check_republished: Check = False,
    ) -> str:
        """Day-ahead electricity prices for Poland in PLN/MWh: RCE and/or SDAC. Daily and monthly rows give
        mean, min, max and hours with price <= 0."""
        return core.prices(src, start, end, kind, resolution, max_rows, check_republished)

    @tool
    def generation(
        start: Start,
        end: End = None,
        sources: Annotated[list[str] | None,
                           Field(description="his-wlk-cal fields; default wi (wind) and pv. Others, e.g. jgw1, "
                                             "jgm, jnwrb: see describe_report('his-wlk-cal')")] = None,
        resolution: Resolution = "auto",
        agg: Annotated[Literal["mean", "min", "max"], Field(description="Statistic per row")] = "mean",
        max_rows: MaxRows = 200,
        check_republished: Check = False,
    ) -> str:
        """Generation in MW: wind and PV totals, or PSE unit-type categories (PSE publishes no fuel split here)."""
        return core.generation(src, start, end, sources, resolution, agg, max_rows, check_republished)

    @tool
    def demand(
        start: Start,
        end: End = None,
        forecast: Annotated[bool, Field(description="Also return PSE's load forecast")] = False,
        resolution: Resolution = "auto",
        max_rows: MaxRows = 200,
        check_republished: Check = False,
    ) -> str:
        """Load of the Polish power system in MW. Daily and monthly rows give mean, min and max."""
        return core.demand(src, start, end, forecast, resolution, max_rows, check_republished)

    @tool
    def cross_border_flows(
        start: Start,
        end: End = None,
        neighbours: Annotated[list[str] | None,
                              Field(description="Country codes, e.g. DE, CZ, SK, SE, LT, UA; default: all")] = None,
        resolution: Resolution = "auto",
        components: Annotated[bool, Field(description="Also return the import and export components")] = False,
        max_rows: MaxRows = 200,
        check_republished: Check = False,
    ) -> str:
        """Net physical cross-border flow per neighbour in MW (+ = import to Poland). Each border's two
        directional rows are netted. Range limit 184 days."""
        return core.cross_border_flows(src, start, end, neighbours, resolution, components, max_rows,
                                       check_republished)

    @tool
    def res_curtailment(
        start: Start,
        end: End = None,
        resolution: Resolution = "auto",
        max_rows: MaxRows = 200,
        check_republished: Check = False,
    ) -> str:
        """Non-market redispatch (curtailment) of wind and PV ordered by PSE, split by reason (balancing or
        network): MWh per day or month, MW per hour or interval. Available from 2024-09-24."""
        return core.res_curtailment(src, start, end, resolution, max_rows, check_republished)

    @tool
    def summarize(
        report: Annotated[str, Field(description="Report name with one row per interval, e.g. rce-pln, kse-load")],
        start: Start,
        end: End = None,
        fields: Annotated[list[str] | None, Field(description="Fields; default: all")] = None,
        by: Annotated[Literal["total", "day", "month"],
                      Field(description="One row per field for the whole period, or per day or month")] = "total",
        max_rows: MaxRows = 200,
        check_republished: Check = False,
    ) -> str:
        """Statistics of a report's fields: intervals with and without data, mean, min, max, when the extremes
        occurred, and for prices the hours at or below zero. Range limit 1100 days (366 with by=day)."""
        return core.summarize(src, report, start, end, fields, by, max_rows, check_republished)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pse-opendata-mcp", description=__doc__.split("\n")[0])
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--cache-dir", help="directory of the month cache (default: $PSE_OPENDATA_CACHE or the "
                                            "user cache directory)")
    parser.add_argument("--no-cache", action="store_true", help="do not cache downloaded months")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"pse-opendata-mcp {__version__}")
    args = parser.parse_args(argv)
    # stdout carries the protocol on stdio, so logs go to stderr
    logging.basicConfig(level=args.log_level, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    options: dict[str, Any] = {}
    if args.no_cache:
        options["cache_dir"] = None
    elif args.cache_dir:
        options["cache_dir"] = args.cache_dir
    source = core.Source(lambda check: pse.Client(check_republished=check, **options))
    build_server(source).run(transport=args.transport)


if __name__ == "__main__":
    main()
