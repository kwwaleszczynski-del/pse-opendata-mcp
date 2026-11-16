"""Tool logic: load PSE reports through ``pse-opendata`` and render compact, self-describing text.

Every data answer has the same shape: a few header lines (source report, retrieval time, period, time zone,
columns with units, missing data) followed by one CSV table. Units, time zone and source are stated once, never
per row. Values are aggregated unless raw rows are asked for, and tables are capped at ``max_rows``.

Usage:
    from pse_opendata_mcp import core
    src = core.Source()
    print(core.prices(src, "2026-10-24", "2026-10-26", kind="both"))
"""

from __future__ import annotations

import csv
import io
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pse_opendata as pse
from pse_opendata.catalog import CHECKED_ON, REPORTS, Report

TZ_NAME = "Europe/Warsaw"
TZ = ZoneInfo(TZ_NAME)
API_HOST = "api.raporty.pse.pl"

DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 500
# longest range in days per resolution; long ranges must be aggregated
RANGE_LIMIT = {"native": 7, "hour": 31, "day": 366, "month": 1100}
# reports with many rows per interval get shorter ranges
REPORT_LIMIT = {"gen-jw": 7, "popmb-rmb": 1, "przeplywy-mocy": 184}
COARSER = {"native": ["hour", "day", "month"], "hour": ["day", "month"], "day": ["month"], "month": []}
PRICE_REPORTS = {"rce": ("rce-pln", "rce_pln"), "sdac": ("csdac-pln", "csdac_pln")}
CURTAILMENT = {"pv_red_balance": "pv_bal", "pv_red_network": "pv_net",
               "wi_red_balance": "wi_bal", "wi_red_network": "wi_net"}
HOURLY_PRODUCT_END = date(2025, 9, 30)


class InputError(ValueError):
    """The arguments of a tool call cannot be served; the message says how to fix them."""


class DataError(RuntimeError):
    """PSE could not be reached or answered with an error."""


@dataclass
class Loaded:
    """One report loaded for a period, with how fresh the data are."""

    spec: Report
    frame: pd.DataFrame
    fetched: list[datetime]
    cached: bool
    notes: list[str]


class Source:
    """Access to PSE data through ``pse_opendata.Client``, one request at a time.

    ``make_client(check_republished)`` builds the client; the default uses the library's month cache
    (``$PSE_OPENDATA_CACHE`` or the user cache directory).
    """

    def __init__(self, make_client: Callable[[bool], pse.Client] | None = None):
        self._make = make_client or (lambda check: pse.Client(check_republished=check))
        self._clients: dict[bool, pse.Client] = {}
        self._lock = threading.Lock()

    def load(self, report: str, lo: date, hi: date, fields: Sequence[str] | None = None,
             check_republished: bool = False) -> Loaded:
        try:
            spec = pse.get_report(report)
        except pse.UnknownReportError as e:
            raise InputError(str(e.args[0])) from None
        with self._lock:
            if check_republished not in self._clients:
                self._clients[check_republished] = self._make(check_republished)
            client = self._clients[check_republished]
            client.loads.clear()
            try:
                frame = client.get(report, lo, hi, fields=fields)
            except pse.UnknownFieldError as e:
                raise InputError(str(e)) from None
            except pse.PseError as e:
                raise DataError(f"PSE request for {report} failed: {e}") from e
            loads = list(client.loads)
        notes = []
        if lo < spec.first_date <= hi:
            notes.append(f"{spec.name} starts on {spec.first_date}; earlier days are not available here")
        if any(m.status == "stale" for m in loads):
            notes.append("download failed; an older cached copy was used")
        if not all(m.complete for m in loads):
            notes.append("cached copy is older than the end of the period; later intervals may be missing")
        return Loaded(spec, frame, [m.fetched_utc for m in loads], any(m.status == "cached" for m in loads), notes)


# --- arguments ----------------------------------------------------------------------------------------------------


def parse_range(start: str, end: str | None) -> tuple[date, date]:
    """Business dates from ISO text; ``end`` defaults to ``start``."""
    days = []
    for label, text in (("start", start), ("end", end if end else start)):
        try:
            days.append(date.fromisoformat(str(text).strip()))
        except ValueError:
            raise InputError(f"{label} must be a date YYYY-MM-DD (Europe/Warsaw business day), got {text!r}") from None
    lo, hi = days
    if lo > hi:
        raise InputError(f"start {lo} is after end {hi}; swap them")
    return lo, hi


def resolve(resolution: str, lo: date, hi: date, reports: Sequence[str] = ()) -> str:
    """The resolution to use (``auto`` picks by range length) after checking the range limits."""
    days = (hi - lo).days + 1
    if resolution == "auto":
        resolution = "hour" if days <= 2 else "day" if days <= 92 else "month"
    if resolution not in RANGE_LIMIT:
        raise InputError(f"resolution must be one of auto, {', '.join(RANGE_LIMIT)}; got {resolution!r}")

    def limit(res: str) -> int:
        return min([RANGE_LIMIT[res], *(REPORT_LIMIT[r] for r in reports if r in REPORT_LIMIT)])

    if days > limit(resolution):
        coarser = next((r for r in COARSER[resolution] if days <= limit(r)), None)
        hint = f" or use resolution='{coarser}'" if coarser else ""
        which = f" for {', '.join(r for r in reports if r in REPORT_LIMIT)}" if limit(resolution) < RANGE_LIMIT[
            resolution] else ""
        raise InputError(f"range {lo}..{hi} is {days} days; the limit for resolution='{resolution}'{which} is "
                         f"{limit(resolution)} days. Narrow the range{hint}.")
    return resolution


def check_max_rows(max_rows: int) -> int:
    if not 1 <= max_rows <= HARD_MAX_ROWS:
        raise InputError(f"max_rows must be between 1 and {HARD_MAX_ROWS}, got {max_rows}")
    return max_rows


# --- time ---------------------------------------------------------------------------------------------------------


def day_hours(day: date) -> float:
    """Length of a Polish local day in hours (23 or 25 on DST change days)."""
    a = datetime.combine(day, time(), TZ).astimezone(UTC)
    b = datetime.combine(day + timedelta(days=1), time(), TZ).astimezone(UTC)
    return (b - a).total_seconds() / 3600


def dst_note(lo: date, hi: date) -> str:
    days = (lo + timedelta(days=i) for i in range((hi - lo).days + 1))
    odd = [f"{d} has {day_hours(d):g} h" for d in days if d.weekday() == 6 and day_hours(d) != 24]
    return f"; DST change: {', '.join(odd)}" if odd else ""


def _offset(text: str) -> str:
    return f"{text[:3]}:{text[3:]}"


class Stamps:
    """Interval starts as local ISO text; the UTC offset is printed per row only when it changes in the frame."""

    def __init__(self, frame: pd.DataFrame):
        offsets = sorted(set(frame["start_local"].dt.strftime("%z"))) if len(frame) else []
        self.per_row = len(offsets) > 1
        if self.per_row:
            self.note = f"interval start, {TZ_NAME} local time with UTC offset (DST change in the period)"
        elif offsets:
            self.note = f"interval start, {TZ_NAME} local time (UTC{_offset(offsets[0])})"
        else:
            self.note = f"interval start, {TZ_NAME} local time"

    def one(self, stamp: pd.Timestamp) -> str:
        local = stamp.tz_convert(TZ_NAME)
        text = local.strftime("%Y-%m-%dT%H:%M")
        return text + _offset(local.strftime("%z")) if self.per_row else text


def bucket(frame: pd.DataFrame, resolution: str) -> pd.Series:
    if resolution == "native":
        return frame["start_utc"]
    if resolution == "hour":
        return frame["start_utc"].dt.floor("h")      # UTC clock hours are Polish clock hours too
    if resolution == "day":
        return frame["business_date"]
    return frame["business_date"].map(lambda d: f"{d:%Y-%m}")


def bucket_text(key: object, resolution: str, stamps: Stamps) -> str:
    if resolution in ("native", "hour"):
        return stamps.one(key)
    return key.isoformat() if resolution == "day" else str(key)


def period_line(lo: date, hi: date, resolution: str, stamps: Stamps, native: str) -> str:
    span = f"{lo}" if lo == hi else f"{lo}..{hi}"
    if resolution == "native":
        return f"period: {span}; rows: {native} intervals; time: {stamps.note}"
    if resolution == "hour":
        return f"period: {span}; rows: hours; time: {stamps.note}"
    unit = "days" if resolution == "day" else "months"
    return f"period: {span}; rows: {unit} ({TZ_NAME} business days){dst_note(lo, hi)}"


FIRST_COLUMN = {"native": "t", "hour": "t", "day": "day", "month": "month"}

# --- rendering ----------------------------------------------------------------------------------------------------


def decimals(unit: str | None) -> int:
    return 2 if unit and unit.startswith("PLN") else 1


def fmt(value: object, places: int = 1) -> str:
    """A number as short text; missing stays empty (never 0)."""
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, str):
        return value
    text = f"{float(value):.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def source_line(loaded: Sequence[Loaded], processed: str, check_republished: bool = False) -> str:
    names = ", ".join(item.spec.name for item in loaded)
    fetched = sorted(t for item in loaded for t in item.fetched)
    if not fetched:
        when = f"{datetime.now(UTC):%Y-%m-%dT%H:%MZ}"
    elif fetched[0].date() == fetched[-1].date():
        when = f"{fetched[0]:%Y-%m-%dT%H:%MZ}"
    else:
        when = f"{fetched[0]:%Y-%m-%d}..{fetched[-1]:%Y-%m-%dT%H:%MZ}"
    extra = " (month cache)" if any(item.cached for item in loaded) else ""
    extra += "; republication check on" if check_republished else ""
    plural = "s" if len(loaded) > 1 else ""
    return f"source: PSE S.A. report{plural} {names} ({API_HOST}); retrieved {when}{extra}; processed: {processed}"


def missing_line(frame: pd.DataFrame, fields: dict[str, str]) -> list[str]:
    parts = [f"{alias} {n}/{len(frame)}" for name, alias in fields.items() if (n := int(frame[name].isna().sum()))]
    if not parts:
        return []
    return [f"missing: {', '.join(parts)} intervals empty at source (left out of aggregates, never filled)"]


def render(head: Sequence[str], columns: Sequence[str], rows: Sequence[Sequence[str]], max_rows: int,
           empty: str = "no data for this period") -> str:
    """Header lines and a CSV table; rows beyond ``max_rows`` are cut with an explicit note."""
    lines = [line for line in head if line]
    if not rows:
        return "\n".join([*lines, empty])
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows[:max_rows])
    lines.append(out.getvalue().rstrip("\n"))
    if len(rows) > max_rows:
        lines.append(f"truncated: first {max_rows} of {len(rows)} rows shown; narrow the range or aggregate "
                     f"(coarser resolution)")
    return "\n".join(lines)


def empty_reason(item: Loaded, hi: date) -> str:
    if hi < item.spec.first_date:
        return (f"no data: {item.spec.name} starts on {item.spec.first_date} (earlier history is in PSE's legacy "
                f"export, which this server does not query)")
    return "no data for this period (not published yet, or outside the report's range)"


def aggregate(frame: pd.DataFrame, resolution: str, fields: Sequence[str], stats: Sequence[str],
              interval_hours: float, by: Sequence[str] = ()) -> pd.DataFrame:
    """One row per bucket (and ``by`` value); columns ``(field, stat)``. Missing values are left out of every
    statistic and a bucket without any value stays missing."""
    work = frame[[*by, *fields]].copy()
    work["_bucket"] = bucket(frame, resolution).to_numpy()
    groups = work.groupby(["_bucket", *by], sort=True)
    out = {}
    for name in fields:
        count = groups[name].count()
        for stat in stats:
            if stat == "sum":
                out[(name, stat)] = groups[name].sum(min_count=1)
            elif stat == "le0_h":
                at_or_below = (work[name] <= 0).groupby([work[c] for c in ["_bucket", *by]], sort=True).sum()
                out[(name, stat)] = (at_or_below * interval_hours).where(count > 0)
            else:
                out[(name, stat)] = getattr(groups[name], stat)()
    return pd.DataFrame(out)


def series_table(frame: pd.DataFrame, resolution: str, fields: dict[str, str], stats: Sequence[str],
                 places: dict[str, int], interval_hours: float, stamps: Stamps,
                 by: Sequence[str] = ()) -> tuple[list[str], list[list[str]]]:
    """Columns and text rows of a time series table. ``fields`` maps report fields to short column names."""
    first = FIRST_COLUMN[resolution]
    if resolution == "native":
        columns = [first, *by, *fields.values()]
        rows = [[stamps.one(t), *(fmt(v) for v in keys), *(fmt(v, places[f]) for f, v in zip(fields, values,
                                                                                               strict=True))]
                for t, keys, values in zip(frame["start_utc"], frame[list(by)].itertuples(index=False),
                                           frame[list(fields)].itertuples(index=False), strict=True)]
        return columns, rows
    table = aggregate(frame, resolution, list(fields), stats, interval_hours, by)
    if len(stats) == 1:
        names = list(fields.values())
    elif len(fields) == 1:
        names = list(stats)
    else:
        names = [f"{alias}_{stat}" for alias in fields.values() for stat in stats]
    spec = [(f, s, 2 if s == "le0_h" else places[f]) for f in fields for s in stats]
    rows = []
    for index, values in zip(table.index, table.itertuples(index=False), strict=True):
        key, *keys = index if isinstance(index, tuple) else (index,)
        rows.append([bucket_text(key, resolution, stamps), *(fmt(k) for k in keys),
                     *(fmt(v, p) for v, (_, _, p) in zip(values, spec, strict=True))])
    return [first, *by, *names], rows


def units_text(spec: Report, fields: dict[str, str]) -> str:
    units = {name: spec.field(name).unit or "-" for name in fields}
    if len(set(units.values())) == 1:
        return f"{', '.join(fields.values())} in {next(iter(units.values()))}"
    return ", ".join(f"{alias} {units[name]}" for name, alias in fields.items())


def stat_text(stats: Sequence[str], resolution: str) -> str:
    if resolution == "native":
        return "values as published"
    per = {"hour": "hour", "day": "day", "month": "month"}[resolution]
    return f"{'/'.join(s for s in stats if s != 'le0_h')} per {per}"


# --- tools --------------------------------------------------------------------------------------------------------


def list_reports() -> str:
    """The catalogue: one line per report."""
    head = [f"source: PSE S.A. reports API ({API_HOST}); catalogue of pse-opendata {pse.__version__}, checked "
            f"against PSE on {CHECKED_ON}",
            "use describe_report(name) for fields and caveats; dates are Europe/Warsaw business days"]
    rows = []
    for r in REPORTS.values():
        units = sorted({f.unit for f in r.fields if f.unit})
        rows.append([r.name, r.resolution, r.first_date.isoformat(), ";".join(units), len(r.fields), r.title])
    return render(head, ["report", "interval", "from", "units", "fields", "title"], rows, HARD_MAX_ROWS)


def describe_report(report: str) -> str:
    """Fields, units and caveats of one report."""
    try:
        r = pse.get_report(report)
    except pse.UnknownReportError as e:
        raise InputError(str(e.args[0])) from None
    head = [f"report: {r.name}: {r.title} ({r.title_pl})",
            f"source: PSE S.A., {r.url}; catalogue of pse-opendata {pse.__version__}, checked on {CHECKED_ON}",
            f"about: {r.description}",
            f"interval: {r.resolution}; from: {r.first_date}; PSE may republish up to ~{r.revision_days} days "
            f"after a month ends"]
    if r.key:
        head.append(f"several rows per interval, told apart by: {', '.join(r.key)}")
    head += [f"note: {note}" for note in r.notes]
    rows = [[f.name, f.unit or "", f.description + ("" if f.unit_verified else " (unit not confirmed by PSE)")]
            for f in r.fields]
    return render(head, ["field", "unit", "meaning"], rows, HARD_MAX_ROWS)


def get_report(src: Source, report: str, start: str, end: str | None = None, fields: Sequence[str] | None = None,
               resolution: str = "auto", agg: str | None = None, max_rows: int = DEFAULT_MAX_ROWS,
               check_republished: bool = False) -> str:
    """Any catalogued report as a time series."""
    lo, hi = parse_range(start, end)
    max_rows = check_max_rows(max_rows)
    try:
        spec = pse.get_report(report)
    except pse.UnknownReportError as e:
        raise InputError(str(e.args[0])) from None
    if spec.name == "przeplywy-mocy":
        raise InputError("przeplywy-mocy has two directional rows per border that must be netted; use "
                         "cross_border_flows instead")
    if not spec.unique_key and resolution == "auto":
        resolution = "native"
    resolution = resolve(resolution, lo, hi, [spec.name])
    if not spec.unique_key and resolution != "native":
        raise InputError(f"{spec.name} holds individual rows that cannot be aggregated over time; use "
                         f"resolution='native' for a single day")
    if agg not in (None, "mean", "sum", "min", "max"):
        raise InputError(f"agg must be mean, sum, min or max; got {agg!r}")
    item = src.load(spec.name, lo, hi, fields or None, check_republished)
    frame = item.frame
    present = [c for c in frame.columns if c not in pse.TIME_COLUMNS]
    numeric = {c: c for c in present if spec.field(c).dtype == "float"}
    text = [c for c in present if spec.field(c).dtype == "str"]
    places = {c: decimals(spec.field(c).unit) for c in numeric}
    stamps = Stamps(frame)
    hours = spec.interval / pd.Timedelta("1h")
    if resolution == "native":
        stats: list[str] = []
        by = text
    else:
        if not numeric:
            raise InputError(f"no numeric field selected; fields of {spec.name}: {', '.join(spec.field_names)}")
        default = "sum" if all(spec.field(c).unit == "MWh" for c in numeric) else "mean"
        stats = [agg or default]
        by = [c for c in text if c in spec.key]
    columns, rows = series_table(frame, resolution, numeric, stats, places, hours, stamps, by)
    head = [source_line([item], stat_text(stats, resolution), check_republished),
            period_line(lo, hi, resolution, stamps, spec.resolution),
            f"columns: {units_text(spec, numeric)}" if numeric else "",
            *missing_line(frame, numeric), *(f"note: {n}" for n in item.notes)]
    return render(head, columns, rows, max_rows, empty_reason(item, hi))


def prices(src: Source, start: str, end: str | None = None, kind: str = "rce", resolution: str = "auto",
           max_rows: int = DEFAULT_MAX_ROWS, check_republished: bool = False) -> str:
    """RCE and/or SDAC day-ahead price for Poland."""
    if kind not in ("rce", "sdac", "both"):
        raise InputError(f"kind must be rce, sdac or both; got {kind!r}")
    kinds = ["rce", "sdac"] if kind == "both" else [kind]
    lo, hi = parse_range(start, end)
    max_rows = check_max_rows(max_rows)
    resolution = resolve(resolution, lo, hi)
    loaded = [src.load(PRICE_REPORTS[k][0], lo, hi, None, check_republished) for k in kinds]
    frame = loaded[0].frame
    for item in loaded[1:]:
        frame = frame.merge(item.frame, on=pse.TIME_COLUMNS, how="outer").sort_values("start_utc")
    fields = {PRICE_REPORTS[k][1]: k for k in kinds}
    stats = ["mean"] if resolution == "hour" else ["mean", "min", "max", "le0_h"]
    stamps = Stamps(frame)
    columns, rows = series_table(frame, resolution, fields, stats, dict.fromkeys(fields, 2), 0.25, stamps)
    legend = {"rce": "rce = RCE (market price of electricity)", "sdac": "sdac = SDAC day-ahead coupling price"}
    what = "; ".join(legend[k] for k in kinds) + ", PLN/MWh"
    if resolution in ("day", "month"):
        what += "; le0_h = hours with price <= 0"
    notes = [n for item in loaded for n in item.notes]
    if resolution == "native" and lo <= HOURLY_PRODUCT_END:
        notes.append("hourly product until 2025-09-30: PSE repeats the hourly price in four quarter-hours")
    head = [source_line(loaded, stat_text(stats, resolution), check_republished),
            period_line(lo, hi, resolution, stamps, "15min"), f"columns: {what}",
            *missing_line(frame, fields), *(f"note: {n}" for n in notes)]
    return render(head, columns, rows, max_rows, empty_reason(loaded[0], hi))


def _fundamental(src: Source, report: str, fields: dict[str, str], lo: date, hi: date, resolution: str,
                 stats: Sequence[str], max_rows: int, check_republished: bool, notes: Sequence[str] = ()) -> str:
    item = src.load(report, lo, hi, list(fields), check_republished)
    stamps = Stamps(item.frame)
    places = dict.fromkeys(fields, 1)
    columns, rows = series_table(item.frame, resolution, fields, stats, places, 0.25, stamps)
    head = [source_line([item], stat_text(stats, resolution), check_republished),
            period_line(lo, hi, resolution, stamps, item.spec.resolution),
            f"columns: {units_text(item.spec, fields)}",
            *missing_line(item.frame, fields), *(f"note: {n}" for n in [*item.notes, *notes])]
    return render(head, columns, rows, max_rows, empty_reason(item, hi))


def generation(src: Source, start: str, end: str | None = None, sources: Sequence[str] | None = None,
               resolution: str = "auto", agg: str = "mean", max_rows: int = DEFAULT_MAX_ROWS,
               check_republished: bool = False) -> str:
    """Generation by source or unit type from ``his-wlk-cal``."""
    if agg not in ("mean", "min", "max"):
        raise InputError(f"agg must be mean, min or max; got {agg!r}")
    lo, hi = parse_range(start, end)
    max_rows = check_max_rows(max_rows)
    resolution = resolve(resolution, lo, hi)
    fields = {name: name for name in (sources or ["wi", "pv"])}
    notes = [] if set(fields) <= {"wi", "pv"} else ["unit-type fields overlap (e.g. jg includes jgz); do not add them"]
    return _fundamental(src, "his-wlk-cal", fields, lo, hi, resolution, [agg], max_rows, check_republished, notes)


def demand(src: Source, start: str, end: str | None = None, forecast: bool = False, resolution: str = "auto",
           max_rows: int = DEFAULT_MAX_ROWS, check_republished: bool = False) -> str:
    """Actual (and forecast) load of the Polish power system from ``kse-load``."""
    lo, hi = parse_range(start, end)
    max_rows = check_max_rows(max_rows)
    resolution = resolve(resolution, lo, hi)
    fields = {"load_actual": "actual"}
    notes = []
    if forecast:
        fields["load_fcst"] = "fcst"
        notes.append("fcst is the value PSE publishes with the report, possibly after the fact")
    stats = ["mean"] if resolution == "hour" else ["mean", "min", "max"]
    return _fundamental(src, "kse-load", fields, lo, hi, resolution, stats, max_rows, check_republished, notes)


def cross_border_flows(src: Source, start: str, end: str | None = None, neighbours: Sequence[str] | None = None,
                       resolution: str = "auto", components: bool = False, max_rows: int = DEFAULT_MAX_ROWS,
                       check_republished: bool = False) -> str:
    """Net physical flow per neighbouring country: the two directional rows of each border are added."""
    lo, hi = parse_range(start, end)
    max_rows = check_max_rows(max_rows)
    resolution = resolve(resolution, lo, hi, ["przeplywy-mocy"])
    item = src.load("przeplywy-mocy", lo, hi, None, check_republished)
    net = pse.net_flows(item.frame)
    known = sorted(net["neighbour"].unique())
    if neighbours:
        wanted = [str(n).upper() for n in neighbours]
        unknown = [n for n in wanted if n not in known]
        if unknown and known:
            raise InputError(f"unknown neighbours {unknown}; available in this period: {', '.join(known)}")
        net = net[net["neighbour"].isin(wanted)].reset_index(drop=True)
    stamps = Stamps(net)
    measures = ["import", "export", "net"]
    means = aggregate(net, resolution, measures, ["mean"], 0.25, ["neighbour"]) if len(net) else pd.DataFrame()
    first = FIRST_COLUMN[resolution]
    if not len(net):
        columns, rows = [], []
    elif components:
        columns = [first, "neighbour", *measures]
        rows = [[bucket_text(key, resolution, stamps), neighbour, *(fmt(v) for v in values)]
                for (key, neighbour), values in zip(means.index, means.itertuples(index=False), strict=True)]
    else:
        wide = means[("net", "mean")].unstack("neighbour")
        wide["total"] = wide.sum(axis=1, skipna=False)
        columns = [first, *wide.columns]
        rows = [[bucket_text(key, resolution, stamps), *(fmt(v) for v in values)]
                for key, values in zip(wide.index, wide.itertuples(index=False), strict=True)]
    what = ("import = X-PL row (>= 0), export = PL-X row (<= 0), net = import + export"
            if components else "net flow per neighbour (X-PL row + PL-X row) and their total")
    per = "as published" if resolution == "native" else f"mean per {resolution}"
    head = [source_line([item], f"directional rows netted per neighbour, {per}", check_republished),
            period_line(lo, hi, resolution, stamps, "15min"),
            f"columns: {what}, MW; + = import to Poland, - = export",
            *missing_line(net, {"net": "net"}), *(f"note: {n}" for n in item.notes)]
    return render(head, columns, rows, max_rows, empty_reason(item, hi))


def res_curtailment(src: Source, start: str, end: str | None = None, resolution: str = "auto",
                    max_rows: int = DEFAULT_MAX_ROWS, check_republished: bool = False) -> str:
    """Non-market redispatch of wind and PV from ``poze-redoze``."""
    lo, hi = parse_range(start, end)
    max_rows = check_max_rows(max_rows)
    resolution = resolve(resolution, lo, hi)
    item = src.load("poze-redoze", lo, hi, None, check_republished)
    fields = list(CURTAILMENT)
    frame = item.frame.dropna(subset=fields, how="all")
    stamps = Stamps(item.frame)
    energy = resolution in ("day", "month")
    rows: list[list[str]] = []
    columns = [FIRST_COLUMN[resolution], *CURTAILMENT.values(), "total", *(["h"] if energy else [])]
    if len(frame):
        work = frame.copy()
        if energy:
            work[fields] = work[fields] * 0.25
        table = aggregate(work, resolution, fields, ["sum" if energy else "mean"], 0.25)
        table.columns = fields
        table["total"] = table.sum(axis=1, min_count=1)
        if energy:
            table["h"] = work.groupby(bucket(work, resolution).to_numpy(), sort=True).size() * 0.25
        rows = [[bucket_text(key, resolution, stamps), *(fmt(v, 2 if c == "h" else 1) for c, v in
                                                         zip(table.columns, values, strict=True))]
                for key, values in zip(table.index, table.itertuples(index=False), strict=True)]
    unit = ("MWh (MW x 0.25 h summed); h = hours with any redispatch" if energy
            else "MW" + ("" if resolution == "native" else ", mean of the intervals with redispatch"))
    head = [source_line([item], "energy per " + resolution if energy else stat_text(["mean"], resolution),
                        check_republished),
            period_line(lo, hi, resolution, stamps, "15min"),
            f"columns: pv/wi = PV/wind, bal = redispatch for balancing, net = for network constraints, in {unit}; "
            f"sign as published by PSE (negative = generation reduced)",
            f"{len(frame)} of {len(item.frame)} intervals have redispatch; periods without any are omitted "
            f"(empty at source = none reported as of retrieval)",
            *(f"note: {n}" for n in item.notes)]
    if hi < item.spec.first_date or not len(item.frame):
        return render(head[:2], columns, [], max_rows, empty_reason(item, hi))
    return render(head, columns, rows, max_rows, "no redispatch reported in this period")


def summarize(src: Source, report: str, start: str, end: str | None = None, fields: Sequence[str] | None = None,
              by: str = "total", max_rows: int = DEFAULT_MAX_ROWS, check_republished: bool = False) -> str:
    """Statistics per field: count, missing, mean, min, max and, for prices, hours at or below zero."""
    if by not in ("total", "day", "month"):
        raise InputError(f"by must be total, day or month; got {by!r}")
    lo, hi = parse_range(start, end)
    max_rows = check_max_rows(max_rows)
    try:
        spec = pse.get_report(report)
    except pse.UnknownReportError as e:
        raise InputError(str(e.args[0])) from None
    if spec.key:
        tool = "cross_border_flows" if spec.name == "przeplywy-mocy" else "get_report"
        raise InputError(f"{spec.name} has several rows per interval; use {tool}")
    resolve("day" if by == "day" else "month", lo, hi, [spec.name])
    item = src.load(spec.name, lo, hi, fields or None, check_republished)
    frame = item.frame
    names = {c: c for c in frame.columns if c not in pse.TIME_COLUMNS and spec.field(c).dtype == "float"}
    stamps = Stamps(frame)
    hours = spec.interval / pd.Timedelta("1h")
    price = any(spec.field(c).unit == "PLN/MWh" for c in names)
    total = by == "total"
    columns = [*([] if total else [by]), "field", "n", "miss", "mean", "min", "max",
               *(["t_min", "t_max"] if total else []), *(["le0_h", "neg_h"] if price else [])]
    groups = [("", frame)] if total else list(frame.groupby(bucket(frame, by).to_numpy(), sort=True))
    rows = []
    for key, part in groups:
        for name in names:
            values = part[name]
            n = int(values.count())
            places = decimals(spec.field(name).unit)
            row = [*([] if total else [bucket_text(key, by, stamps)]), name, str(n), str(len(values) - n),
                   fmt(values.mean(), places), fmt(values.min(), places), fmt(values.max(), places)]
            if total:
                row += [stamps.one(part.loc[values.idxmin(), "start_utc"]) if n else "",
                        stamps.one(part.loc[values.idxmax(), "start_utc"]) if n else ""]
            if price:
                is_price = spec.field(name).unit == "PLN/MWh" and n
                row += [fmt((values <= 0).sum() * hours, 2) if is_price else "",
                        fmt((values < 0).sum() * hours, 2) if is_price else ""]
            rows.append(row)
    legend = f"n/miss = {spec.resolution} intervals with/without a value"
    if total:
        legend += f"; t_min/t_max = {stamps.note}"
    if price:
        legend += "; le0_h/neg_h = hours with price <= 0 / < 0"
    span = f"{lo}" if lo == hi else f"{lo}..{hi}"
    head = [source_line([item], "summary statistics", check_republished),
            f"period: {span} ({TZ_NAME} business days); rows: {'whole period' if total else 'per ' + by}"
            f"{dst_note(lo, hi)}",
            f"columns: {units_text(spec, names)}; {legend}" if names else "",
            *(f"note: {n}" for n in item.notes)]
    return render(head, columns, rows if len(frame) else [], max_rows, empty_reason(item, hi))
