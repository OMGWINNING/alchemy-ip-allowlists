"""Unit tests for check_alchemy_prod_version_bump.py pure helpers."""

from __future__ import annotations

from pathlib import Path

from ci.check_alchemy_prod_version_bump import (  # noqa: E402
    changed_alchemy_chart_dirs,
    chart_dir_for_path,
    is_render_affecting_chart_path,
    pinned_version_from_config,
    read_chart_version_from_text,
)
from ci.check_prod_stage_alignment import is_concrete_pin  # noqa: E402


def test_chart_dir_for_path():
    assert chart_dir_for_path(Path("alchemy-observability-core/values.yaml")) == (
        "alchemy-observability-core"
    )
    assert chart_dir_for_path(Path("alchemy-observability-shard/templates/x.yaml")) == (
        "alchemy-observability-shard"
    )
    assert chart_dir_for_path(Path("loki/values.yaml")) is None
    assert chart_dir_for_path(Path("helm/obs/prod/x/config.yaml")) is None


def test_is_render_affecting_chart_path():
    assert is_render_affecting_chart_path(Path("alchemy-observability-core/values.yaml"))
    assert is_render_affecting_chart_path(Path("alchemy-observability-core/templates/a.yaml"))
    assert is_render_affecting_chart_path(Path("alchemy-observability-shard/patches/x.yaml"))
    assert is_render_affecting_chart_path(Path("alchemy-observability-core/Chart.yaml"))
    assert not is_render_affecting_chart_path(Path("alchemy-observability-core/README.md"))
    assert not is_render_affecting_chart_path(Path("alchemy-observability-core/.helmignore"))
    assert not is_render_affecting_chart_path(Path("loki/values.yaml"))


def test_changed_alchemy_chart_dirs_dedupes_and_ignores_docs():
    changed = [
        Path("alchemy-observability-core/templates/a.yaml"),
        Path("alchemy-observability-core/values.yaml"),
        Path("alchemy-observability-core/README.md"),
        Path("loki/values.yaml"),
    ]
    assert changed_alchemy_chart_dirs(changed) == {"alchemy-observability-core"}

    assert changed_alchemy_chart_dirs([Path("alchemy-observability-core/README.md")]) == set()


def test_is_concrete_pin():
    assert is_concrete_pin("0.0.22")
    assert not is_concrete_pin(">=0.0.22")
    assert not is_concrete_pin("0.0.22-rc1")


def test_read_chart_version_from_text():
    raw = "apiVersion: v2\nname: x\nversion: 0.0.22\n"
    assert read_chart_version_from_text(raw) == "0.0.22"
    assert read_chart_version_from_text("version: broken: yaml: [") is None
    assert read_chart_version_from_text(None) is None


def test_pinned_version_from_config():
    assert pinned_version_from_config({"grafanaChartVersion": "0.0.8"}, "grafana") == "0.0.8"
    assert pinned_version_from_config({"chartVersion": "0.0.22"}, "core") == "0.0.22"
    assert pinned_version_from_config({}, "agent") is None
