"""Tool logic on recorded PSE responses: aggregation, DST days, missing data, netting, truncation, bad input."""

from __future__ import annotations

from datetime import date

import pytest

from pse_opendata_mcp import core

from conftest import Recorded, recorded, table


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def day_values(fixture: str, field: str, day: str) -> list[float | None]:
    return [r[field] for r in recorded(fixture) if r["business_date"] == day]


# --- header: source, retrieval time, units, time zone -------------------------------------------------------------


def test_header_states_source_retrieval_units_and_zone_once(src: core.Source) -> None:
    text = core.prices(src, "2026-10-24", "2026-10-26", resolution="day")
    head, columns, rows = table(text)
    assert head[0] == ("source: PSE S.A. report rce-pln (api.raporty.pse.pl); retrieved 2026-11-16T12:00Z; "
                       "processed: mean/min/max per day")
    assert "Europe/Warsaw business days" in head[1]
    assert "PLN/MWh" in head[2] and "le0_h = hours with price <= 0" in head[2]
    assert columns == ["day", "mean", "min", "max", "le0_h"]
    assert [r[0] for r in rows] == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert text.count("PLN/MWh") == 1 and text.count("Europe/Warsaw") == 1 and text.count("PSE S.A.") == 1


def test_daily_price_statistics_match_the_recorded_rows(src: core.Source) -> None:
    _, _, rows = table(core.prices(src, "2026-10-24", "2026-10-26", resolution="day"))
    values = day_values("rce-pln_2026-10-24_2026-10-26", "rce_pln", "2026-10-25")
    assert len(values) == 100
    row = rows[1]
    assert float(row[1]) == pytest.approx(mean(values), abs=0.005)
    assert float(row[2]) == min(values) and float(row[3]) == max(values)
    assert float(row[4]) == sum(v <= 0 for v in values) * 0.25 > 0


def test_both_prices_side_by_side(src: core.Source) -> None:
    head, columns, rows = table(core.prices(src, "2026-10-25", kind="both", resolution="day"))
    assert "reports rce-pln, csdac-pln" in head[0]
    assert columns == ["day", "rce_mean", "rce_min", "rce_max", "rce_le0_h",
                       "sdac_mean", "sdac_min", "sdac_max", "sdac_le0_h"]
    sdac = day_values("csdac-pln_2026-10-24_2026-10-26", "csdac_pln", "2026-10-25")
    assert float(rows[0][5]) == pytest.approx(mean(sdac), abs=0.005)


# --- DST days -----------------------------------------------------------------------------------------------------


def test_autumn_dst_day_has_25_hours_and_unambiguous_stamps(src: core.Source) -> None:
    head, _, rows = table(core.prices(src, "2026-10-25", resolution="hour"))
    assert len(rows) == 25
    stamps = [r[0] for r in rows]
    assert stamps[2:4] == ["2026-10-25T02:00+02:00", "2026-10-25T02:00+01:00"]
    assert len(set(stamps)) == 25
    assert "with UTC offset (DST change in the period)" in head[1]


def test_autumn_dst_day_has_100_quarter_hours(src: core.Source) -> None:
    text = core.prices(src, "2026-10-25", resolution="native")
    _, _, rows = table(text)
    assert len(rows) == 100 and "truncated" not in text
    assert "rows: 15min intervals" in text


def test_spring_dst_day_has_23_hours(src: core.Source) -> None:
    head, _, rows = table(core.prices(src, "2026-03-29", resolution="hour"))
    stamps = [r[0] for r in rows]
    assert len(rows) == 23
    assert stamps[1:3] == ["2026-03-29T01:00+01:00", "2026-03-29T03:00+02:00"]
    daily = core.prices(src, "2026-03-29", resolution="day")
    assert "DST change: 2026-03-29 has 23 h" in daily


def test_dst_note_lists_the_days_with_their_length(src: core.Source) -> None:
    text = core.demand(src, "2026-10-24", "2026-10-26")
    assert "DST change: 2026-10-25 has 25 h" in text
    assert core.day_hours(date(2026, 10, 25)) == 25 and core.day_hours(date(2026, 3, 29)) == 23
    assert core.dst_note(date(2026, 4, 1), date(2026, 9, 30)) == ""


def test_offset_is_stated_once_when_it_does_not_change(src: core.Source) -> None:
    head, _, rows = table(core.prices(src, "2026-10-24", resolution="hour"))
    assert rows[0][0] == "2026-10-24T00:00" and len(rows) == 24
    assert "Europe/Warsaw local time (UTC+02:00)" in head[1]


def test_hourly_values_are_means_of_the_quarter_hours(src: core.Source) -> None:
    _, _, rows = table(core.prices(src, "2026-10-24", resolution="hour"))
    values = day_values("rce-pln_2026-10-24_2026-10-26", "rce_pln", "2026-10-24")
    assert float(rows[0][1]) == pytest.approx(mean(values[:4]), abs=0.005)
    assert float(rows[23][1]) == pytest.approx(mean(values[92:]), abs=0.005)


# --- missing data -------------------------------------------------------------------------------------------------


def test_missing_values_stay_missing(src: core.Source) -> None:
    raw = recorded("his-wlk-cal_2026-11-16_partial")
    empty = sum(r["demand"] is None for r in raw)
    assert 0 < empty < len(raw)
    text = core.generation(src, "2026-11-16", sources=["demand", "wi"], resolution="native")
    head, columns, rows = table(text)
    assert columns == ["t", "demand", "wi"] and len(rows) == 96
    assert sum(r[1] == "" for r in rows) == empty
    assert not any(r[1] == "0" for r in rows)
    assert f"missing: demand {empty}/96, wi {empty}/96 intervals empty at source" in text


def test_aggregates_leave_missing_values_out(src: core.Source) -> None:
    present = [r["demand"] for r in recorded("his-wlk-cal_2026-11-16_partial") if r["demand"] is not None]
    _, _, rows = table(core.generation(src, "2026-11-16", sources=["demand"], resolution="day"))
    assert float(rows[0][1]) == pytest.approx(mean(present), abs=0.05)
    hourly = table(core.generation(src, "2026-11-16", sources=["demand"], resolution="hour"))[2]
    assert hourly[-1][1] == "" and hourly[0][1] != ""


def test_summary_counts_missing_intervals(src: core.Source) -> None:
    raw = recorded("his-wlk-cal_2026-11-16_partial")
    empty = sum(r["pv"] is None for r in raw)
    _, columns, rows = table(core.summarize(src, "his-wlk-cal", "2026-11-16", fields=["pv"]))
    row = dict(zip(columns, rows[0], strict=True))
    assert row["field"] == "pv" and int(row["n"]) == 96 - empty and int(row["miss"]) == empty


def test_period_without_data_says_so(src: core.Source) -> None:
    assert "no data for this period" in core.demand(src, "2026-12-24")
    early = core.prices(src, "2020-01-01", "2020-01-02")
    assert "no data: rce-pln starts on 2024-06-14" in early
    assert "no data: poze-redoze starts on 2024-09-24" in core.res_curtailment(src, "2024-07-01")


# --- cross-border flows -------------------------------------------------------------------------------------------


def test_flows_are_netted_per_neighbour(src: core.Source) -> None:
    raw = recorded("przeplywy-mocy_2026-10-25")
    inbound = [r["value"] for r in raw if r["section_code"] == "CZ-PL"]
    outbound = [r["value"] for r in raw if r["section_code"] == "PL-CZ"]
    assert len(inbound) == len(outbound) == 100
    head, columns, rows = table(core.cross_border_flows(src, "2026-10-25", resolution="day"))
    row = dict(zip(columns, rows[0], strict=True))
    net = mean(inbound) + mean(outbound)
    assert float(row["CZ"]) == pytest.approx(net, abs=0.05)
    assert abs(net - mean(inbound)) > 100          # a single directional row would be far off
    assert columns == ["day", "CZ", "DE", "LT", "SE", "SK", "UA", "total"]
    assert float(row["total"]) == pytest.approx(sum(r["value"] for r in raw) / 100, abs=0.5)
    assert "+ = import to Poland" in head[2] and "netted" in head[0]


def test_flow_components_and_neighbour_filter(src: core.Source) -> None:
    _, columns, rows = table(core.cross_border_flows(src, "2026-10-25", neighbours=["cz"], components=True,
                                                     resolution="day"))
    assert columns == ["day", "neighbour", "import", "export", "net"]
    assert len(rows) == 1 and rows[0][1] == "CZ"
    imp, exp, net = (float(v) for v in rows[0][2:])
    assert imp >= 0 >= exp and net == pytest.approx(imp + exp, abs=0.11)
    with pytest.raises(core.InputError, match="unknown neighbours .*available in this period: CZ, DE"):
        core.cross_border_flows(src, "2026-10-25", neighbours=["FR"])


def test_generic_tool_refuses_directional_rows(src: core.Source) -> None:
    with pytest.raises(core.InputError, match="use cross_border_flows"):
        core.get_report(src, "przeplywy-mocy", "2026-10-25")
    with pytest.raises(core.InputError, match="use cross_border_flows"):
        core.summarize(src, "przeplywy-mocy", "2026-10-25")


# --- curtailment --------------------------------------------------------------------------------------------------


def test_curtailment_energy_per_day(src: core.Source) -> None:
    raw = [r for r in recorded("poze-redoze_2026-10-24_2026-10-26") if r["business_date"] == "2026-10-25"]
    wind = [r["wi_red_balance"] for r in raw if r["wi_red_balance"] is not None]
    assert wind
    text = core.res_curtailment(src, "2026-10-24", "2026-10-26")
    _, columns, rows = table(text)
    row = dict(zip(columns, next(r for r in rows if r[0] == "2026-10-25"), strict=True))
    assert float(row["wi_bal"]) == pytest.approx(sum(wind) * 0.25, abs=0.05)
    assert row["pv_net"] == "" and row["wi_net"] == ""
    any_value = sum(any(r[f] is not None for f in core.CURTAILMENT) for r in raw)
    assert float(row["h"]) == any_value * 0.25
    assert "MWh" in text and "negative = generation reduced" in text


def test_curtailment_omits_intervals_without_redispatch(src: core.Source) -> None:
    raw = [r for r in recorded("poze-redoze_2026-10-24_2026-10-26") if r["business_date"] == "2026-10-25"]
    with_value = sum(any(r[f] is not None for f in core.CURTAILMENT) for r in raw)
    text = core.res_curtailment(src, "2026-10-25", resolution="native")
    assert len(table(text)[2]) == with_value < 100
    assert f"{with_value} of 100 intervals have redispatch" in text


# --- summary ------------------------------------------------------------------------------------------------------


def test_summary_of_prices(src: core.Source) -> None:
    values = [r["rce_pln"] for r in recorded("rce-pln_2026-10-24_2026-10-26")]
    _, columns, rows = table(core.summarize(src, "rce-pln", "2026-10-24", "2026-10-26"))
    row = dict(zip(columns, rows[0], strict=True))
    assert int(row["n"]) == 292 and row["miss"] == "0"
    assert float(row["min"]) == min(values) and float(row["max"]) == max(values)
    assert float(row["le0_h"]) == sum(v <= 0 for v in values) * 0.25
    assert float(row["neg_h"]) == sum(v < 0 for v in values) * 0.25
    assert row["t_min"].startswith("2026-10-25T") and row["t_min"].endswith("+01:00")


def test_summary_by_day_has_one_row_per_day_and_field(src: core.Source) -> None:
    _, columns, rows = table(core.summarize(src, "kse-load", "2026-10-24", "2026-10-26", by="day"))
    assert columns == ["day", "field", "n", "miss", "mean", "min", "max"]
    assert [(r[0], r[1], r[2]) for r in rows[2:4]] == [("2026-10-25", "load_fcst", "100"),
                                                         ("2026-10-25", "load_actual", "100")]


# --- generic report -----------------------------------------------------------------------------------------------


def test_get_report_aggregates_selected_fields(src: core.Source) -> None:
    text = core.get_report(src, "kse-load", "2026-10-24", "2026-10-26", fields=["load_actual"], agg="max")
    head, columns, rows = table(text)
    assert columns == ["day", "load_actual"] and "processed: max per day" in head[0]
    assert float(rows[1][1]) == pytest.approx(max(day_values("kse-load_2026-10-24_2026-10-26", "load_actual",
                                                             "2026-10-25")), abs=0.05)
    assert "columns: load_actual in MW" in text


# --- truncation and limits ----------------------------------------------------------------------------------------


def test_long_tables_are_truncated_with_a_note(src: core.Source) -> None:
    text = core.prices(src, "2026-10-24", "2026-10-26", resolution="native")
    assert len(table(text)[2]) == 200
    assert text.endswith("truncated: first 200 of 292 rows shown; narrow the range or aggregate (coarser resolution)")
    short = core.prices(src, "2026-10-24", "2026-10-26", resolution="native", max_rows=5)
    assert len(table(short)[2]) == 5 and "first 5 of 292 rows" in short


def test_auto_resolution_follows_the_range() -> None:
    assert core.resolve("auto", date(2026, 1, 1), date(2026, 1, 2)) == "hour"
    assert core.resolve("auto", date(2026, 1, 1), date(2026, 3, 31)) == "day"
    assert core.resolve("auto", date(2025, 1, 1), date(2026, 3, 31)) == "month"


@pytest.mark.parametrize(("call", "message"), [
    (lambda s: core.prices(s, "2026-10-26", "2026-10-24"), "start 2026-10-26 is after end 2026-10-24"),
    (lambda s: core.prices(s, "26.10.2026"), "start must be a date YYYY-MM-DD"),
    (lambda s: core.prices(s, "2026-10-24", "yesterday"), "end must be a date YYYY-MM-DD"),
    (lambda s: core.prices(s, "2025-01-01", "2026-10-26", resolution="hour"),
     r"is 664 days; the limit for resolution='hour' is 31 days. Narrow the range or use resolution='month'"),
    (lambda s: core.prices(s, "2026-10-01", "2026-10-20", resolution="native"),
     r"limit for resolution='native' is 7 days. Narrow the range or use resolution='hour'"),
    (lambda s: core.demand(s, "2024-06-14", "2026-10-26", resolution="day"),
     r"limit for resolution='day' is 366 days. Narrow the range or use resolution='month'"),
    (lambda s: core.demand(s, "2020-01-01", "2026-10-26"), r"limit for resolution='month' is 1100 days. Narrow"),
    (lambda s: core.cross_border_flows(s, "2025-01-01", "2025-12-31"),
     r"limit for resolution='month' for przeplywy-mocy is 184 days"),
    (lambda s: core.get_report(s, "gen-jw", "2026-10-01", "2026-10-20"), "for gen-jw is 7 days"),
    (lambda s: core.get_report(s, "popmb-rmb", "2026-10-25", resolution="day"), "cannot be aggregated over time"),
    (lambda s: core.prices(s, "2026-10-24", max_rows=0), "max_rows must be between 1 and 500"),
    (lambda s: core.prices(s, "2026-10-24", max_rows=5000), "max_rows must be between 1 and 500"),
    (lambda s: core.prices(s, "2026-10-24", kind="spot"), "kind must be rce, sdac or both"),
    (lambda s: core.get_report(s, "rce", "2026-10-24"), "unknown report 'rce'; known reports: .*rce-pln"),
    (lambda s: core.get_report(s, "rce-pln", "2026-10-24", fields=["price"]), "unknown fields .*available"),
    (lambda s: core.generation(s, "2026-10-24", sources=["coal"]), "unknown fields .*available"),
    (lambda s: core.summarize(s, "rce-pln", "2026-10-24", by="week"), "by must be total, day or month"),
    (lambda s: core.describe_report("nope"), "unknown report 'nope'"),
])
def test_bad_input_is_rejected_with_a_helpful_message(src: core.Source, rec: Recorded, call, message: str) -> None:
    with pytest.raises(core.InputError, match=message):
        call(src)
    assert rec.session.calls == []          # rejected before any request


# --- republication check, catalogue -------------------------------------------------------------------------------


def test_republication_check_is_passed_to_the_library(rec: Recorded) -> None:
    plain = core.prices(rec.source, "2026-10-25", resolution="day")
    checked = core.prices(rec.source, "2026-10-25", resolution="day", check_republished=True)
    assert rec.checks == [False, True]
    assert "republication check on" in checked and "republication check" not in plain
    assert rec.source._clients[True].check_republished and not rec.source._clients[False].check_republished


def test_catalogue_tools() -> None:
    _, columns, rows = table(core.list_reports())
    assert columns == ["report", "interval", "from", "units", "fields", "title"]
    assert {r[0] for r in rows} == set(core.REPORTS)
    text = core.describe_report("przeplywy-mocy")
    assert "several rows per interval, told apart by: section_code" in text and "net_flows" in text
    assert "rce_pln,PLN/MWh," in core.describe_report("rce-pln")


def test_numbers_are_short_and_missing_is_empty() -> None:
    assert core.fmt(612.0, 2) == "612" and core.fmt(-11.26, 2) == "-11.26" and core.fmt(0.04, 1) == "0"
    assert core.fmt(-0.04, 1) == "0" and core.fmt(float("nan")) == "" and core.fmt(None) == ""
    assert core.fmt(18234.513) == "18234.5"
