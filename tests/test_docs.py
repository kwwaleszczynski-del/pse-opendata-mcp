"""The documentation that is generated from the code must match the code."""

from __future__ import annotations

import json

import gen_readme
import pse_opendata_mcp


def test_readme_tool_reference_matches_the_server() -> None:
    current = gen_readme.README.read_text(encoding="utf-8")
    assert gen_readme.replace_block(current, "tools", gen_readme.tools_reference()) == current, \
        "run: python scripts/gen_readme.py tools"


def test_live_outputs_cover_every_tool() -> None:
    text = gen_readme.LIVE.read_text(encoding="utf-8")
    tools = {tool for tool, _ in gen_readme.MORE} | {tool for _, tool, _ in gen_readme.QUESTIONS}
    assert len(tools) == 9
    for tool in tools:
        assert f"Call: `{tool}(" in text or f"`{tool}(" in gen_readme.README.read_text(encoding="utf-8"), tool


def test_release_metadata_agrees() -> None:
    """Version in the package, server.json, CITATION.cff and CHANGELOG.md; registry name in server.json and README."""
    root = gen_readme.ROOT
    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    (package,) = server["packages"]
    assert server["version"] == package["version"] == pse_opendata_mcp.__version__
    assert (package["registryType"], package["identifier"]) == ("pypi", "pse-opendata-mcp")
    assert len(server["description"]) <= 100                      # limit of the registry schema
    assert f"\nversion: {pse_opendata_mcp.__version__}\n" in (root / "CITATION.cff").read_text(encoding="utf-8")
    assert f"\n## [{pse_opendata_mcp.__version__}] - " in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    # the registry verifies ownership by this marker in the README that PyPI shows
    assert f"<!-- mcp-name: {server['name']} -->" in gen_readme.README.read_text(encoding="utf-8")
