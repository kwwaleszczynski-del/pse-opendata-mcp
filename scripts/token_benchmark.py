"""Measure the size in tokens of the server's answers against two JSON alternatives, on the live PSE API.

For each of the three README questions it compares:

* compact: the text the tool returns (header lines and one CSV table);
* same rows as JSON: the same aggregated table as a JSON array of objects (keys repeated in every row), with the
  header as a ``meta`` object, i.e. the same information in the usual tool-output shape;
* raw API JSON: the records the PSE API returns for the same report and period, passed through unchanged, which
  is what a thin wrapper around the API would hand to the model.

Tokens are counted with tiktoken's ``o200k_base`` encoding. Tokenizers differ between models, so the counts are
approximate; the ratios move little. The tool list (sent to the model once per session) is counted too.

Usage:
    pip install -e ".[bench]"
    python scripts/token_benchmark.py              # print the table
    python scripts/token_benchmark.py --write      # also write it into README.md
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
from datetime import UTC, datetime

import pse_opendata as pse
import tiktoken
from mcp import Client

from gen_readme import QUESTIONS, README, call_text, live, replace_block
from pse_opendata_mcp import core
from pse_opendata_mcp.server import build_server

log = logging.getLogger("token_benchmark")
ENCODING = "o200k_base"
REPORT_OF = {"prices": "rce-pln", "cross_border_flows": "przeplywy-mocy", "res_curtailment": "poze-redoze"}


def as_json(text: str) -> str:
    """The same answer as JSON: header lines as ``meta``, the table as an array of objects."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.split(",")[0] in core.FIRST_COLUMN.values())
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    records = [{k: float(v) if _number(v) else (None if v == "" else v) for k, v in row.items()} for row in rows]
    return json.dumps({"meta": lines[:start], "rows": records})


def _number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def raw_api_json(client: pse.Client, report: str, start: str, end: str) -> tuple[str, int]:
    """The API's own records for the period, as one JSON document."""
    records = client.records(report, filter=f"business_date ge '{start}' and business_date le '{end}'")
    return json.dumps({"value": records}, ensure_ascii=False), len(records)


async def tool_list_json() -> str:
    async with Client(build_server()) as client:
        tools = (await client.list_tools()).tools
    return json.dumps([t.model_dump(exclude_none=True) for t in tools])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="write the table into README.md")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    encoding = tiktoken.get_encoding(ENCODING)

    def tokens(text: str) -> int:
        return len(encoding.encode(text))

    answers = live([(tool, a) for _, tool, a in QUESTIONS])
    client = pse.Client(cache_dir=None)
    lines = ["| Question | Tool call | Compact answer | Same rows as JSON | Raw API JSON | Raw / compact |",
             "|---|---|---:|---:|---:|---:|"]
    for (question, tool, a), (text, error) in zip(QUESTIONS, answers, strict=True):
        if error:
            raise SystemExit(f"{tool} failed: {text}")
        raw, count = raw_api_json(client, REPORT_OF[tool], a["start"], a["end"])
        compact, same, full = tokens(text), tokens(as_json(text)), tokens(raw)
        log.info("%s: compact %d, json %d, raw %d tokens (%d API rows)", tool, compact, same, full, count)
        lines.append(f"| {question} | `{call_text(tool, a)}` | {compact:,} | {same:,} | {full:,} ({count:,} rows) "
                     f"| {full / compact:,.0f}x |")
    tool_list = asyncio.run(tool_list_json())
    stamp = f"{datetime.now(UTC):%Y-%m-%d}"
    body = "\n".join([
        *lines, "",
        f"Measured on {stamp} against the live API with `python scripts/token_benchmark.py`; tokens counted with "
        f"tiktoken `{ENCODING}` (approximate: tokenizers differ between models). The definitions of all tools, "
        f"which a client sends to the model once per session, take {tokens(tool_list):,} tokens.",
    ])
    print(body)
    if args.write:
        README.write_text(replace_block(README.read_text(encoding="utf-8"), "tokens", body), encoding="utf-8",
                          newline="\n")
        log.info("wrote the token table into README.md")


if __name__ == "__main__":
    main()
