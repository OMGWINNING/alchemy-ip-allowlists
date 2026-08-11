"""Unit tests for helm_quality.py pure logic (no helm/gh required).

Covers guard matching, the grep-backed forbidden-pattern detector, and
chart-root discovery. (PR-comment preview/pagination moved to the shared
helm-diff CLI in cloud-infra-tools and is tested there.)
"""

from __future__ import annotations

from pathlib import Path

from ci import helm_quality as hq  # noqa: E402


# --------------------------------------------------------------------------- #
# guard matching
# --------------------------------------------------------------------------- #
def test_guard_applies_by_chart_name():
    guard = {"match_chart_name": "alchemy-observability-shard"}
    assert hq.guard_applies(guard, "alchemy-observability-shard", [])
    assert not hq.guard_applies(guard, "something-else", [])


def test_guard_applies_by_dependency():
    guard = {"match_chart_dependency": "alchemy-observability-shard"}
    assert hq.guard_applies(
        guard, "per-instance-core", [{"name": "alchemy-observability-shard", "version": "0.0.23"}]
    )
    assert not hq.guard_applies(guard, "per-instance-core", [{"name": "loki", "version": "1.2.3"}])


def test_guard_applies_by_dependency_version():
    guard = {
        "match_chart_dependency": "alchemy-observability-core",
        "match_chart_dependency_version": "0.0.23",
    }
    assert hq.guard_applies(
        guard, "per-instance-core", [{"name": "alchemy-observability-core", "version": "0.0.23"}]
    )
    assert not hq.guard_applies(
        guard, "per-instance-core", [{"name": "alchemy-observability-core", "version": "0.0.22"}]
    )


def test_guard_applies_neither_field_set():
    assert not hq.guard_applies({}, "anything", ["dep"])


def test_guard_applies_empty_string_does_not_match_empty_name():
    # An empty match_chart_name must not accidentally match a chart with no name.
    assert not hq.guard_applies({"match_chart_name": ""}, "", [])


# --------------------------------------------------------------------------- #
# forbidden-pattern detection (shells out to grep, like the workflow did)
# --------------------------------------------------------------------------- #
def test_grep_hits_matches_retention_size():
    text = "spec:\n  retentionSize: 40GiB\n  other: value\n"
    hits = hq._grep_hits(text, r"retentionSize:[[:space:]]*['\"]?[^[:space:]'\"#]")
    assert any("retentionSize: 40GiB" in h for h in hits)


def test_grep_hits_strips_full_line_comments():
    text = "# retentionSize: 40GiB is fine in a comment\nfoo: bar\n"
    hits = hq._grep_hits(text, r"retentionSize:[[:space:]]*['\"]?[^[:space:]'\"#]")
    assert hits == []


def test_grep_hits_double_dash_pattern():
    # `--`-prefixed patterns must not be parsed as grep options.
    text = "args:\n  - --storage.tsdb.retention.size=40GB\n"
    hits = hq._grep_hits(text, r"--storage\.tsdb\.retention\.size")
    assert any("retention.size" in h for h in hits)


def test_grep_hits_no_match():
    assert hq._grep_hits("nothing interesting here\n", r"retentionSize:") == []


def test_check_guards_fails_missing_required_pattern(tmp_path):
    chartfile = tmp_path / "Chart.yaml"
    chartfile.write_text("name: alchemy-observability-core\n")
    guard = {
        "id": "probe-contract",
        "description": "core probe discovery contract",
        "match_chart_name": "alchemy-observability-core",
        "required_patterns": [r"probeNamespaceSelector:[[:space:]]*\{\}"],
    }

    logs, ok = hq._check_guards(chartfile, "kind: Prometheus\n", "core", [guard])

    assert not ok
    assert any("did not match required pattern" in log for log in logs)


def test_check_guards_passes_required_pattern(tmp_path):
    chartfile = tmp_path / "Chart.yaml"
    chartfile.write_text("name: alchemy-observability-core\n")
    guard = {
        "id": "probe-contract",
        "description": "core probe discovery contract",
        "match_chart_name": "alchemy-observability-core",
        "required_patterns": [r"probeNamespaceSelector:[[:space:]]*\{\}"],
    }

    logs, ok = hq._check_guards(
        chartfile, "kind: Prometheus\n  probeNamespaceSelector: {}\n", "core", [guard]
    )

    assert ok
    assert logs == []


# --------------------------------------------------------------------------- #
# chart discovery
# --------------------------------------------------------------------------- #
def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("name: x\n")


def test_find_chart_roots_excludes_vendored_and_template_dirs(tmp_path):
    _touch(tmp_path / "cert-manager" / "Chart.yaml")
    _touch(tmp_path / "helm" / "obs" / "core" / "Chart.yaml")
    _touch(tmp_path / "cert-manager" / "charts" / "sub" / "Chart.yaml")  # vendored
    _touch(tmp_path / "alchemy-observability-shard" / "patches" / "Chart.yaml")  # template
    _touch(tmp_path / ".git" / "Chart.yaml")
    _touch(tmp_path / ".github" / "Chart.yaml")

    roots = {p.relative_to(tmp_path).as_posix() for p in hq.find_chart_roots(tmp_path)}
    assert roots == {"cert-manager/Chart.yaml", "helm/obs/core/Chart.yaml"}
