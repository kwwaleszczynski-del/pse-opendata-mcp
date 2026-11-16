# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/).

## [0.1.0] - 2026-11-16

First release.

### Added

- MCP server `pse-opendata-mcp` (stdio by default, `--transport streamable-http` for HTTP) on top of the
  `pse-opendata` library, with nine tools: `list_reports`, `describe_report`, `get_report`, `prices`, `generation`,
  `demand`, `cross_border_flows`, `res_curtailment` and `summarize`.
- Compact answers: a few header lines (source report, retrieval time, time zone, units) and one CSV table;
  aggregated by default, raw rows capped at 200 (500 at most) with an explicit `truncated:` line; over-long ranges
  are rejected before any request is made.
- Polish business days in `Europe/Warsaw` local time, with the UTC offset per row on 23- and 25-hour days; missing
  values stay missing; cross-border flows netted per neighbour; `check_republished` to ask PSE whether cached
  months have changed.
- Offline test suite on recorded PSE responses, a script that generates the README tool reference and the live
  examples, and a token benchmark.
- Packaging: release workflow for PyPI with Trusted Publishing, and `server.json` with the `mcp-name` marker in
  the README for the MCP registry.
