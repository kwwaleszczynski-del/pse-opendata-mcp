"""Record real PSE responses used by the offline tests (tests/fixtures/*.json).

Each fixture is one query of the reports API, trimmed to the fields the tests need. Records are stored as the API
returned them. Re-recording changes values the tests assert only where PSE republished the data.

Usage:
    python scripts/record_fixtures.py
    python scripts/record_fixtures.py --name rce-pln_2026-10-24_2026-10-26
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pse_opendata as pse

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
log = logging.getLogger("record_fixtures")
AUTUMN = "business_date ge '2026-10-24' and business_date le '2026-10-26'"     # 2026-10-25 has 25 hours

# name -> (endpoint, OData filter, fields)
FIXTURES = {
    "rce-pln_2026-10-24_2026-10-26": ("rce-pln", AUTUMN, ["rce_pln"]),
    "csdac-pln_2026-10-24_2026-10-26": ("csdac-pln", AUTUMN, ["csdac_pln"]),
    "kse-load_2026-10-24_2026-10-26": ("kse-load", AUTUMN, ["load_fcst", "load_actual"]),
    "his-wlk-cal_2026-10-24_2026-10-26": ("his-wlk-cal", AUTUMN, ["demand", "wi", "pv"]),
    "poze-redoze_2026-10-24_2026-10-26": ("poze-redoze", AUTUMN, list(pse.get_report("poze-redoze").field_names)),
    "przeplywy-mocy_2026-10-25": ("przeplywy-mocy", "business_date eq '2026-10-25'", ["section_code", "value"]),
    "rce-pln_2026-03-29": ("rce-pln", "business_date eq '2026-03-29'", ["rce_pln"]),      # 23 hours
    # recorded during the day: the remaining intervals of the day are empty at the source
    "his-wlk-cal_2026-11-16_partial": ("his-wlk-cal", "business_date eq '2026-11-16'", ["demand", "wi", "pv"]),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--name", choices=sorted(FIXTURES), help="record one fixture only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = pse.Client(cache_dir=None)
    ROOT.mkdir(parents=True, exist_ok=True)
    for name, (endpoint, flt, fields) in FIXTURES.items():
        if args.name and name != args.name:
            continue
        select = ["dtime_utc", "business_date", *fields, "publication_ts_utc"]
        rows = client.records(endpoint, filter=flt, select=select)
        payload = {"endpoint": endpoint, "filter": flt, "recorded_utc": f"{datetime.now(UTC):%Y-%m-%dT%H:%MZ}",
                   "value": rows}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        (ROOT / f"{name}.json").write_text(text + "\n", encoding="utf-8", newline="\n")
        log.info("%s: %d rows, %d kB", name, len(rows), len(text) // 1024)


if __name__ == "__main__":
    main()
