"""Shared test helpers: recorded PSE responses served by a fake HTTP session. Nothing here touches the network.

Tests marked ``live`` run only with ``pytest --live``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pse_opendata as pse
import pytest

from pse_opendata_mcp import core

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="run tests that call the real PSE servers")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def recorded(name: str) -> list[dict[str, Any]]:
    """Records of one fixture (see scripts/record_fixtures.py)."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["value"]


def all_recorded() -> dict[str, list[dict[str, Any]]]:
    """Every recorded row, per endpoint."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(FIXTURES.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.setdefault(payload["endpoint"], []).extend(payload["value"])
    return rows


class FakeResponse:
    status_code = 200
    reason = "OK"
    headers: dict[str, str] = {}

    def __init__(self, payload: Any):
        self.content = json.dumps(payload).encode("utf-8")

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content)


class FakeSession:
    """Stands in for ``requests.Session``: answers ``business_date`` filters from the recorded rows."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]]):
        self.rows = rows
        self.calls: list[str] = []

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> FakeResponse:
        endpoint = urlparse(url).path.rsplit("/", 1)[-1]
        flt = (params or {}).get("$filter") or parse_qs(urlparse(url).query).get("$filter", [""])[0]
        self.calls.append(f"{endpoint}?{flt}")
        dates = [part.split("'")[1] for part in flt.split(" and ")]
        lo, hi = (dates[0], dates[0]) if " eq " in flt else (dates[0], dates[1])
        return FakeResponse({"value": [r for r in self.rows.get(endpoint, []) if lo <= r["business_date"] <= hi]})


class Recorded:
    """A :class:`core.Source` over the recorded responses; remembers which clients were asked for."""

    def __init__(self) -> None:
        self.session = FakeSession(all_recorded())
        self.checks: list[bool] = []
        self.source = core.Source(self._client)

    def _client(self, check_republished: bool) -> pse.Client:
        self.checks.append(check_republished)
        return pse.Client(session=self.session, cache_dir=None, now=lambda: NOW, sleep=lambda s: None,
                          min_interval=0, check_republished=check_republished)


@pytest.fixture
def rec() -> Recorded:
    return Recorded()


@pytest.fixture
def src(rec: Recorded) -> core.Source:
    return rec.source


def table(text: str) -> tuple[list[str], list[str], list[list[str]]]:
    """Split a tool answer into header lines, column names and rows (the truncation line is left out)."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if "," in line and ":" not in line.split(",")[0]
                 and " " not in line.split(",")[0])
    body = [line for line in lines[start + 1:] if not line.startswith("truncated:")]
    return lines[:start], lines[start].split(","), [line.split(",") for line in body]
