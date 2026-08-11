"""Unit tests for version gate findings + PR comment formatting."""

from __future__ import annotations

from pathlib import Path

from ci.check_alchemy_prod_version_bump import next_patch_version  # noqa: E402
from lib.version_gate_findings import (  # noqa: E402
    VersionGateFinding,
    append_findings,
    format_markdown,
    read_findings,
)


def test_next_patch_version():
    assert next_patch_version("0.0.22") == "0.0.23"
    assert next_patch_version("0.1.9") == "0.1.10"


def test_format_markdown_includes_fix_steps():
    findings = [
        VersionGateFinding(
            check="prod-immutability",
            title="Bump chart",
            file="alchemy-observability-core/Chart.yaml",
            summary="Version 0.0.22 is prod-pinned.",
            fix_steps=["Run bump script.", "Commit Chart.yaml."],
        )
    ]
    md = format_markdown(findings)
    assert "Version gate failures" not in md  # header added by poster, not formatter
    assert "Bump chart" in md
    assert "Run bump script." in md
    assert "`alchemy-observability-core/Chart.yaml`" in md


def test_append_findings_roundtrip(tmp_path: Path):
    path = tmp_path / "findings.jsonl"
    finding = VersionGateFinding(
        check="stage-alignment",
        title="Align stage",
        summary="Prod ahead of stage.",
        fix_steps=["Rebase."],
        file="helm/x/prod/y/config.yaml",
    )
    append_findings(path, [finding])
    assert read_findings(path) == [finding]
