"""The documentation that is generated from the code must match the code."""

from __future__ import annotations

import gen_readme


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
