"""Generate the parts of the documentation that must come from the code or from real runs.

``tools`` writes the tool reference in README.md from the server's own tool list (offline; ``--check`` fails if
the README is out of date, which a test also does). ``examples`` starts the server as a stdio subprocess, calls
every tool against the live PSE API and writes the outputs to README.md (three questions) and
docs/live_outputs.md (every tool). Nothing in those blocks is typed by hand.

Usage:
    python scripts/gen_readme.py tools
    python scripts/gen_readme.py tools --check
    python scripts/gen_readme.py examples
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from pse_opendata_mcp.server import build_server

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
LIVE = ROOT / "docs" / "live_outputs.md"
log = logging.getLogger("gen_readme")

# question, tool, arguments: the three README examples (also used by scripts/token_benchmark.py)
QUESTIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("How did the RCE price develop month by month in 2026, and how many hours were at or below zero?",
     "prices", {"start": "2026-01-01", "end": "2026-10-31", "resolution": "month"}),
    ("Which neighbours did Poland import from and export to in the week of 9-15 November 2026?",
     "cross_border_flows", {"start": "2026-11-09", "end": "2026-11-15"}),
    ("How much wind and PV did PSE curtail in October 2026, and on which days?",
     "res_curtailment", {"start": "2026-10-01", "end": "2026-10-31"}),
]

# every tool at least once, plus a DST day, a truncated table and two rejected calls
MORE: list[tuple[str, dict[str, Any]]] = [
    ("list_reports", {}),
    ("describe_report", {"report": "przeplywy-mocy"}),
    ("prices", {"start": "2026-10-24", "end": "2026-10-26", "kind": "both", "resolution": "day"}),
    ("prices", {"start": "2026-10-25", "resolution": "hour", "max_rows": 6}),
    ("prices", {"start": "2026-10-24", "end": "2026-10-26", "resolution": "native", "max_rows": 4}),
    ("demand", {"start": "2026-11-09", "end": "2026-11-15", "forecast": True}),
    ("generation", {"start": "2026-11-09", "end": "2026-11-15"}),
    ("generation", {"start": "2026-06-01", "end": "2026-10-31", "sources": ["wi", "pv", "jgm"], "agg": "max"}),
    ("cross_border_flows", {"start": "2026-10-25", "neighbours": ["CZ", "DE"], "components": True,
                            "resolution": "day"}),
    ("res_curtailment", {"start": "2026-10-25", "max_rows": 6}),
    ("summarize", {"report": "rce-pln", "start": "2026-10-01", "end": "2026-10-31"}),
    ("summarize", {"report": "his-wlk-cal", "start": "2026-08-01", "end": "2026-10-31",
                   "fields": ["demand", "wi", "pv"], "by": "month"}),
    ("get_report", {"report": "crb-rozl", "start": "2026-10-01", "end": "2026-10-07", "fields": ["cen_cost"]}),
    ("get_report", {"report": "cmbp-tp", "start": "2026-10-25", "fields": ["afrr_g", "afrr_d"], "resolution": "day"}),
    ("get_report", {"report": "gen-jw", "start": "2026-10-25", "resolution": "day", "max_rows": 5}),
    ("prices", {"start": "2025-01-01", "end": "2026-10-31", "resolution": "hour"}),
    ("get_report", {"report": "przeplywy-mocy", "start": "2026-10-25"}),
]

# with a month cache: a finished month is cached by the first call and checked for republication by the second
CACHED: list[tuple[str, dict[str, Any]]] = [
    ("prices", {"start": "2026-08-01", "end": "2026-08-31", "resolution": "month"}),
    ("prices", {"start": "2026-08-01", "end": "2026-08-31", "resolution": "month", "check_republished": True}),
]


def replace_block(text: str, name: str, body: str) -> str:
    """Replace what stands between ``<!-- name:start -->`` and ``<!-- name:end -->``."""
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    if start not in text or end not in text:
        raise SystemExit(f"README.md has no {start} ... {end} block")
    head, rest = text.split(start, 1)
    return f"{head}{start}\n{body.strip()}\n{end}{rest.split(end, 1)[1]}"


def call_text(tool: str, args: dict[str, Any]) -> str:
    return f"{tool}({', '.join(f'{k}={json.dumps(v)}' for k, v in args.items())})"


def _type(prop: dict[str, Any]) -> str:
    options = [o for o in prop.get("anyOf", [prop]) if o.get("type") != "null"]
    kinds = []
    for o in options:
        if "enum" in o:
            kinds.append(" \\| ".join(f"`{v}`" for v in o["enum"]))
        elif o.get("type") == "array":
            kinds.append(f"list of {o.get('items', {}).get('type', 'value')}s")
        else:
            kinds.append(o.get("type", "value"))
    return " or ".join(kinds)


def _default(name: str, prop: dict[str, Any], required: list[str]) -> str:
    if name in required:
        return "required"
    value = prop.get("default")
    return "" if value is None else f"`{json.dumps(value)}`"


async def _tools() -> list[Any]:
    async with Client(build_server()) as client:
        return (await client.list_tools()).tools


def tools_reference() -> str:
    """Markdown reference of every tool, from the definitions the server sends to clients."""
    parts = []
    for tool in asyncio.run(_tools()):
        description = " ".join(tool.description.split())
        parts.append(f"### `{tool.name}`\n\n{description}\n")
        props = tool.input_schema.get("properties", {})
        if not props:
            parts.append("No parameters.\n")
            continue
        required = tool.input_schema.get("required", [])
        rows = ["| Parameter | Type | Default | Meaning |", "|---|---|---|---|"]
        rows += [f"| `{name}` | {_type(prop)} | {_default(name, prop, required)} | {prop.get('description', '')} |"
                 for name, prop in props.items()]
        parts.append("\n".join(rows) + "\n")
    return "\n".join(parts)


async def _live(calls: list[tuple[str, dict[str, Any]]], extra: list[str]) -> list[tuple[str, bool]]:
    """Answers of a server started as a stdio subprocess, from the live PSE API."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "pse_opendata_mcp", *extra])
    out = []
    async with Client(params, read_timeout_seconds=600) as client:
        for tool, args in calls:
            result = await client.call_tool(tool, args)
            log.info("%s -> %d chars%s", call_text(tool, args), len(result.content[0].text),
                     " (error)" if result.is_error else "")
            out.append((result.content[0].text, bool(result.is_error)))
    return out


def live(calls: list[tuple[str, dict[str, Any]]], extra: list[str] | None = None) -> list[tuple[str, bool]]:
    return asyncio.run(_live(calls, extra if extra is not None else ["--no-cache"]))


def _block(tool: str, args: dict[str, Any], text: str, error: bool) -> str:
    label = "Rejected call" if error else "Call"
    return f"{label}: `{call_text(tool, args)}`\n\n```text\n{text}\n```\n"


def write_examples() -> None:
    stamp = f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC"
    answers = live([(tool, args) for _, tool, args in QUESTIONS])
    body = [f"Outputs recorded on {stamp} from a server running over stdio against the live PSE API.\n"]
    for i, ((question, tool, args), (text, error)) in enumerate(zip(QUESTIONS, answers, strict=True), 1):
        body.append(f"**{i}. {question}**\n\n{_block(tool, args, text, error)}")
    README.write_text(replace_block(README.read_text(encoding="utf-8"), "examples", "\n".join(body)),
                      encoding="utf-8", newline="\n")

    more = live(MORE)
    with tempfile.TemporaryDirectory() as cache:
        cached = live(CACHED, ["--cache-dir", cache])
    doc = ["# Live outputs", "",
           f"Every tool called through an MCP client session over stdio against the live PSE API on {stamp}. "
           "Generated by `python scripts/gen_readme.py examples`; nothing here is typed by hand.", "",
           "Information obtained from PSE S.A. (api.raporty.pse.pl), state as of the retrieval time in each answer. "
           "The information has been processed (selected, aggregated, rounded).", ""]
    doc += [_block(tool, args, text, error) for (tool, args), (text, error) in zip(MORE, more, strict=True)]
    doc += ["## Month cache and republication check", "",
            "Server started with `--cache-dir`: the first call downloads and caches a finished month, the second "
            "reuses it and asks PSE whether the month was republished since.", ""]
    doc += [_block(tool, args, text, error) for (tool, args), (text, error) in zip(CACHED, cached, strict=True)]
    LIVE.parent.mkdir(exist_ok=True)
    LIVE.write_text("\n".join(doc), encoding="utf-8", newline="\n")
    log.info("wrote %s and %s", README.name, LIVE.relative_to(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("what", choices=["tools", "examples"])
    parser.add_argument("--check", action="store_true", help="tools: fail if README.md is out of date")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.what == "examples":
        write_examples()
        return
    current = README.read_text(encoding="utf-8")
    updated = replace_block(current, "tools", tools_reference())
    if args.check:
        if updated != current:
            raise SystemExit("README.md tool reference is out of date; run: python scripts/gen_readme.py tools")
        log.info("tool reference is up to date")
    else:
        README.write_text(updated, encoding="utf-8", newline="\n")
        log.info("wrote the tool reference")


if __name__ == "__main__":
    main()
