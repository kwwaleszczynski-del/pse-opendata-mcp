# pse-opendata-mcp

[![tests](https://github.com/kwwaleszczynski-del/pse-opendata-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/kwwaleszczynski-del/pse-opendata-mcp/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io) server that lets an LLM assistant query Polish power-system data: day-ahead
prices (RCE, SDAC), demand, wind and PV generation, net cross-border flows, curtailment of renewables and the
balancing market. The data come from the public reports of **PSE S.A.**, the Polish transmission system operator,
through the [`pse-opendata`](https://github.com/kwwaleszczynski-del/pse-opendata) library.

> This is an independent open-source project. It is not affiliated with, endorsed by or supported by PSE S.A.

## Why another wrapper

A thin wrapper that passes the API's JSON to the model spends the context window on repeated keys and on rows
nobody asked for: one month of quarter-hourly prices is 2,976 records. This server is built around the opposite
default:

- **Aggregated first.** Every tool answers with hourly, daily or monthly rows unless raw intervals are requested;
  raw tables are capped (200 rows by default, 500 at most) and end with an explicit
  `truncated: first 200 of N rows shown` line. Ranges that are too long for the chosen resolution are rejected with
  a message that says what to change, before any request is made.
- **Said once.** Each answer is a few header lines and one CSV table. Source report, retrieval time, time zone and
  units are in the header, never repeated per row; column names are short.
- **Measured.** See [token cost](#token-cost): each of the three example questions below is answered in a few
  hundred tokens, where the raw API JSON for the same period takes hundreds of thousands to millions.

It is equally strict about what the numbers are:

- Dates are Polish business days. Timestamps are interval starts in `Europe/Warsaw` local time; on days when the
  clocks change (23 or 25 hours) every row carries its UTC offset, so the repeated 02:00 hour is unambiguous.
- Missing values stay missing: an empty cell, a `missing:` line with counts, and never a zero. Aggregates use only
  the intervals that have a value.
- Cross-border flows are netted per neighbour from the two directional rows PSE publishes for each border (the
  library's `net_flows`); a single directional row is never reported as the flow.
- Every answer names the PSE report it came from and when it was retrieved. PSE republishes some reports weeks or
  months later; `check_republished=true` asks PSE whether cached months have changed.

## Install

Python 3.11 or newer. The package is not on PyPI yet; install it from the repository:

```bash
pip install git+https://github.com/kwwaleszczynski-del/pse-opendata-mcp
pse-opendata-mcp --version
```

or run it without installing, with [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/kwwaleszczynski-del/pse-opendata-mcp pse-opendata-mcp --version
```

No API key is needed. The first call for a period downloads it from PSE (a second or two per month of data);
finished months are then cached as Parquet files in the user cache directory (`$PSE_OPENDATA_CACHE` or
`--cache-dir` to choose, `--no-cache` to switch off).

## Connect an MCP client

The server speaks MCP over stdio. Most MCP clients take a JSON entry of this shape (the name of the enclosing key
and of the configuration file depends on the client):

```json
{
  "mcpServers": {
    "pse-opendata": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/kwwaleszczynski-del/pse-opendata-mcp", "pse-opendata-mcp"]
    }
  }
}
```

With a `pip` installation use `"command": "pse-opendata-mcp"` and `"args": []`. For clients that connect over HTTP,
`pse-opendata-mcp --transport streamable-http` serves `http://127.0.0.1:8000/mcp`.

## Examples

Three questions, the tool call that answers each and the complete text the model receives.

<!-- examples:start -->
Outputs recorded on 2026-11-16 12:15 UTC from a server running over stdio against the live PSE API.

**1. How did the RCE price develop month by month in 2026, and how many hours were at or below zero?**

Call: `prices(start="2026-01-01", end="2026-10-31", resolution="month")`

```text
source: PSE S.A. report rce-pln (api.raporty.pse.pl); retrieved 2026-11-16T12:15Z; processed: mean/min/max per month
period: 2026-01-01..2026-10-31; rows: months (Europe/Warsaw business days); DST change: 2026-03-29 has 23 h, 2026-10-25 has 25 h
columns: rce = RCE (market price of electricity), PLN/MWh; le0_h = hours with price <= 0
month,mean,min,max,le0_h
2026-01,617.12,-8.94,2118.76,10.25
2026-02,494.3,-2.59,967.26,3.75
2026-03,450.35,-106.32,1199.71,38.5
2026-04,351.33,-891.34,1278.54,98.75
2026-05,424.55,-1457.59,1500.41,64.25
2026-06,486.03,-77.02,2751.72,35.25
2026-07,485.46,-135.17,1727.35,43
2026-08,575.87,-183.33,2218.75,31.75
2026-09,657.23,-66.4,3038.91,22.25
2026-10,738.85,-127.33,2483.15,4.5
```

**2. Which neighbours did Poland import from and export to in the week of 9-15 November 2026?**

Call: `cross_border_flows(start="2026-11-09", end="2026-11-15")`

```text
source: PSE S.A. report przeplywy-mocy (api.raporty.pse.pl); retrieved 2026-11-16T12:16Z; processed: directional rows netted per neighbour, mean per day
period: 2026-11-09..2026-11-15; rows: days (Europe/Warsaw business days)
columns: net flow per neighbour (X-PL row + PL-X row) and their total, MW; + = import to Poland, - = export
day,CZ,DE,LT,SE,SK,UA,total
2026-11-09,-89.3,1979.4,17.1,-133.3,-631.7,-148.5,993.9
2026-11-10,-217.6,1912.2,107.1,59.3,-600,-104.1,1156.9
2026-11-11,-522.1,1475.2,10,-76.8,-834.3,-165.3,-113.3
2026-11-12,-572.2,1653.1,123.2,282.2,-737.4,-104.4,644.5
2026-11-13,-1169.8,818.9,199.7,-123.8,-791.1,18.1,-1048
2026-11-14,-1101.8,533.9,233.2,-241.4,-864.6,-75.8,-1516.6
2026-11-15,-1457,699.3,155.1,-117.9,-1375.5,-262.5,-2358.6
```

**3. How much wind and PV did PSE curtail in October 2026, and on which days?**

Call: `res_curtailment(start="2026-10-01", end="2026-10-31")`

```text
source: PSE S.A. report poze-redoze (api.raporty.pse.pl); retrieved 2026-11-16T12:16Z; processed: energy per day
period: 2026-10-01..2026-10-31; rows: days (Europe/Warsaw business days); DST change: 2026-10-25 has 25 h
columns: pv/wi = PV/wind, bal = redispatch for balancing, net = for network constraints, in MWh (MW x 0.25 h summed); h = hours with any redispatch; sign as published by PSE (negative = generation reduced)
299 of 2980 intervals have redispatch; periods without any are omitted (empty at source = none reported as of retrieval)
day,pv_bal,pv_net,wi_bal,wi_net,total,h
2026-10-01,-2659.2,,-1736.7,,-4395.9,5
2026-10-02,-2498.2,,,,-2498.2,3
2026-10-03,-890.8,,,-5.2,-896,4.75
2026-10-04,-2196.9,,,,-2196.9,4
2026-10-05,-4860.3,,-1432.6,,-6292.9,5
2026-10-06,-1476.9,,-254.5,,-1731.4,3
2026-10-09,-379,,-8488,,-8867,11.75
2026-10-10,-30.4,,-1716.7,,-1747.1,4
2026-10-11,-412.4,,-933,,-1345.4,4
2026-10-14,,,,-70.7,-70.7,2.25
2026-10-16,-2087.1,,,,-2087.1,3
2026-10-23,-300,,,,-300,1
2026-10-24,-261.7,,-237.2,,-498.9,3
2026-10-25,-85.4,,-7325.5,,-7410.9,11
2026-10-26,,,-1113,,-1113,5
2026-10-27,-5714.2,,-868.9,,-6583.1,5
```
<!-- examples:end -->

Outputs of every tool, including a 25-hour day, a truncated table, rejected calls and the republication check:
[docs/live_outputs.md](docs/live_outputs.md).

## Token cost

<!-- tokens:start -->
| Question | Tool call | Compact answer | Same rows as JSON | Raw API JSON | Raw / compact |
|---|---|---:|---:|---:|---:|
| How did the RCE price develop month by month in 2026, and how many hours were at or below zero? | `prices(start="2026-01-01", end="2026-10-31", resolution="month")` | 354 | 577 | 3,824,735 (29,184 rows) | 10,804x |
| Which neighbours did Poland import from and export to in the week of 9-15 November 2026? | `cross_border_flows(start="2026-11-09", end="2026-11-15")` | 373 | 601 | 1,103,808 (8,064 rows) | 2,959x |
| How much wind and PV did PSE curtail in October 2026, and on which days? | `res_curtailment(start="2026-10-01", end="2026-10-31")` | 533 | 1,098 | 436,454 (2,980 rows) | 819x |

Measured on 2026-11-16 against the live API with `python scripts/token_benchmark.py`; tokens counted with tiktoken `o200k_base` (approximate: tokenizers differ between models). The definitions of all tools, which a client sends to the model once per session, take 2,906 tokens.
<!-- tokens:end -->

"Same rows as JSON" is the same aggregated answer written the usual way (an array of objects with the keys repeated
in every row, header lines as a `meta` object). "Raw API JSON" is what the PSE API returns for the same report and
period (every record with all its fields), which is what a pass-through wrapper would hand to the model and
would not fit in a context window at all. Aggregation does most of the work; against the same aggregated rows
written as JSON, the compact format still saves about 40 to 50%.

## Tools

Generated from the tool definitions the server sends to clients (`python scripts/gen_readme.py tools`; a test fails
when this section is out of date). Dates are `YYYY-MM-DD` business days in Polish local time, inclusive.

<!-- tools:start -->
### `list_reports`

List the PSE reports this server can read: name, interval, first date, units, title.

No parameters.

### `describe_report`

Fields, units, row keys and known caveats of one report.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `report` | string | required | Report name, e.g. rce-pln |

### `get_report`

Any catalogued report as a time series (balancing prices and volumes, capacity, plans, unit output). Prefer the dedicated tools for prices, demand, generation, flows and curtailment.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `report` | string | required | Report name from list_reports |
| `start` | string | required | First business day, YYYY-MM-DD |
| `end` | string |  | Last business day, inclusive; default: start |
| `fields` | list of strings |  | Fields to return; default: all |
| `resolution` | `auto` \| `native` \| `hour` \| `day` \| `month` | `"auto"` | Row size. auto: hour up to 2 days, day up to 92, else month. Range limits in days: native 7, hour 31, day 366, month 1100 |
| `agg` | `mean` \| `sum` \| `min` \| `max` |  | Statistic per row; default: sum for MWh fields, else mean |
| `max_rows` | integer | `200` | Row cap, 1-500; longer tables are truncated with a note |
| `check_republished` | boolean | `false` | Ask PSE whether cached months were republished later and reload them |

### `prices`

Day-ahead electricity prices for Poland in PLN/MWh: RCE and/or SDAC. Daily and monthly rows give mean, min, max and hours with price <= 0.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `start` | string | required | First business day, YYYY-MM-DD |
| `end` | string |  | Last business day, inclusive; default: start |
| `kind` | `rce` \| `sdac` \| `both` | `"rce"` | rce: RCE market price; sdac: day-ahead coupling price |
| `resolution` | `auto` \| `native` \| `hour` \| `day` \| `month` | `"auto"` | Row size. auto: hour up to 2 days, day up to 92, else month. Range limits in days: native 7, hour 31, day 366, month 1100 |
| `max_rows` | integer | `200` | Row cap, 1-500; longer tables are truncated with a note |
| `check_republished` | boolean | `false` | Ask PSE whether cached months were republished later and reload them |

### `generation`

Generation in MW: wind and PV totals, or PSE unit-type categories (PSE publishes no fuel split here).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `start` | string | required | First business day, YYYY-MM-DD |
| `end` | string |  | Last business day, inclusive; default: start |
| `sources` | list of strings |  | his-wlk-cal fields; default wi (wind) and pv. Others, e.g. jgw1, jgm, jnwrb: see describe_report('his-wlk-cal') |
| `resolution` | `auto` \| `native` \| `hour` \| `day` \| `month` | `"auto"` | Row size. auto: hour up to 2 days, day up to 92, else month. Range limits in days: native 7, hour 31, day 366, month 1100 |
| `agg` | `mean` \| `min` \| `max` | `"mean"` | Statistic per row |
| `max_rows` | integer | `200` | Row cap, 1-500; longer tables are truncated with a note |
| `check_republished` | boolean | `false` | Ask PSE whether cached months were republished later and reload them |

### `demand`

Load of the Polish power system in MW. Daily and monthly rows give mean, min and max.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `start` | string | required | First business day, YYYY-MM-DD |
| `end` | string |  | Last business day, inclusive; default: start |
| `forecast` | boolean | `false` | Also return PSE's load forecast |
| `resolution` | `auto` \| `native` \| `hour` \| `day` \| `month` | `"auto"` | Row size. auto: hour up to 2 days, day up to 92, else month. Range limits in days: native 7, hour 31, day 366, month 1100 |
| `max_rows` | integer | `200` | Row cap, 1-500; longer tables are truncated with a note |
| `check_republished` | boolean | `false` | Ask PSE whether cached months were republished later and reload them |

### `cross_border_flows`

Net physical cross-border flow per neighbour in MW (+ = import to Poland). Each border's two directional rows are netted. Range limit 184 days.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `start` | string | required | First business day, YYYY-MM-DD |
| `end` | string |  | Last business day, inclusive; default: start |
| `neighbours` | list of strings |  | Country codes, e.g. DE, CZ, SK, SE, LT, UA; default: all |
| `resolution` | `auto` \| `native` \| `hour` \| `day` \| `month` | `"auto"` | Row size. auto: hour up to 2 days, day up to 92, else month. Range limits in days: native 7, hour 31, day 366, month 1100 |
| `components` | boolean | `false` | Also return the import and export components |
| `max_rows` | integer | `200` | Row cap, 1-500; longer tables are truncated with a note |
| `check_republished` | boolean | `false` | Ask PSE whether cached months were republished later and reload them |

### `res_curtailment`

Non-market redispatch (curtailment) of wind and PV ordered by PSE, split by reason (balancing or network): MWh per day or month, MW per hour or interval. Available from 2024-09-24.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `start` | string | required | First business day, YYYY-MM-DD |
| `end` | string |  | Last business day, inclusive; default: start |
| `resolution` | `auto` \| `native` \| `hour` \| `day` \| `month` | `"auto"` | Row size. auto: hour up to 2 days, day up to 92, else month. Range limits in days: native 7, hour 31, day 366, month 1100 |
| `max_rows` | integer | `200` | Row cap, 1-500; longer tables are truncated with a note |
| `check_republished` | boolean | `false` | Ask PSE whether cached months were republished later and reload them |

### `summarize`

Statistics of a report's fields: intervals with and without data, mean, min, max, when the extremes occurred, and for prices the hours at or below zero. Range limit 1100 days (366 with by=day).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `report` | string | required | Report name with one row per interval, e.g. rce-pln, kse-load |
| `start` | string | required | First business day, YYYY-MM-DD |
| `end` | string |  | Last business day, inclusive; default: start |
| `fields` | list of strings |  | Fields; default: all |
| `by` | `total` \| `day` \| `month` | `"total"` | One row per field for the whole period, or per day or month |
| `max_rows` | integer | `200` | Row cap, 1-500; longer tables are truncated with a note |
| `check_republished` | boolean | `false` | Ask PSE whether cached months were republished later and reload them |
<!-- tools:end -->

## Limits

- **Period.** The reports API starts on 2024-06-14 (redispatch on 2024-09-24). Earlier history exists in PSE's
  legacy CSV export, which `pse-opendata` can read but this server does not query: that server takes about a minute
  per month and rate-limits hard, too slow for a tool call.
- **Definitions change.** What RCE is, how it is calculated and what the reports contain is set by PSE and by
  regulation, and has changed before (RCE and SDAC became quarter-hourly products on 2025-10-01; the balancing
  market was reformed on 2024-06-14). The server states which report a number comes from; it does not interpret
  regulation. Check the PSE report page (`describe_report` gives the link) before relying on a definition.
- **Published values change.** PSE republishes reports after corrections: prices rarely, system data for about a
  month, settlement data for several months. An answer is the state of the report at the retrieval time in its
  header. With a long-lived cache use `check_republished=true` (one extra request per report, up to about half a
  minute for cross-border flows).
- **Generation.** `his-wlk-cal` gives wind and PV totals and PSE's unit-type categories, which overlap; there is
  no split by fuel. Curtailment covers only non-market redispatch ordered by PSE, not plants that reduce output on
  their own because of prices.
- **Flows** are physical flows on the commercial cross-sections and exclude local exchange over 110 kV lines.
- **Aggregates** are means (or the stated statistic) over the intervals that have a value. For the current day
  the remaining intervals are empty at the source, so a daily mean covers only the hours so far; the `missing:`
  line says how many intervals were empty. Hours with a price at or below zero are counted in quarter-hours.
- **Load forecast.** `load_fcst` is the value PSE publishes with the report, possibly after the fact; it is not
  necessarily the forecast that was available before delivery.
- **Ranges.** At most 7 days of native rows, 31 days of hours, 366 days of days and 1,100 days of months per
  call; 184 days for cross-border flows, 7 for per-unit generation, 1 for capacity offers.
- **Token counts** depend on the tokenizer of the model; the figures above are approximate.
- **Start-up** takes a few seconds, because the data library loads `pandas`.

## Data source and terms of use

All data come from public reports of **PSE S.A.** (Polskie Sieci Elektroenergetyczne) at
`https://api.raporty.pse.pl/api`. No key is needed. This package does not include or redistribute PSE data, except
small samples of real responses used as test fixtures (see [tests/fixtures/README.md](tests/fixtures/README.md))
and the example outputs in this repository.

PSE publishes conditions for re-using public sector information
([BIP, "Ponowne wykorzystywanie informacji sektora publicznego"](https://www.pse.pl/bip/ponowne-wykorzystanie-informacji-publicznej),
checked for `pse-opendata` on 26 October 2026). They allow commercial and non-commercial re-use free of charge, on
condition that you name the source and the date the information was obtained, keep unchanged information unchanged
and, when you pass on processed information, say that it was changed. PSE accepts no liability for processed
information. The conditions are written for www.pse.pl; the reports API is run by the same operator. Every answer
of this server therefore starts with a line such as

> `source: PSE S.A. report rce-pln (api.raporty.pse.pl); retrieved 2026-11-16T12:00Z; processed: mean/min/max per day`

and the server asks the assistant to keep the source and the retrieval date when it quotes numbers. When you
publish results, add the attribution yourself, for example: "Information obtained from PSE S.A. (report `rce-pln`),
state as of [date]. The information has been processed."

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,bench]"     # Linux/macOS: .venv/bin/python
pytest -q                                   # offline: recorded PSE responses, in-process and stdio client sessions
ruff check .
python scripts/gen_readme.py tools          # refresh the tool reference
python scripts/gen_readme.py examples       # live: re-record the examples and docs/live_outputs.md
python scripts/token_benchmark.py --write   # live: re-measure the token table
```

Built on the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (tested with `mcp` 2.3)
and [`pse-opendata`](https://github.com/kwwaleszczynski-del/pse-opendata) 0.2.1.

## License

Code: MIT, see [LICENSE](LICENSE). Data: PSE S.A. public reports, see
[Data source and terms of use](#data-source-and-terms-of-use).
