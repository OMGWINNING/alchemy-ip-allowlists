"""
Golden-render tests for alchemy-observability-agent prometheus-config-patcher wiring.

Prerequisites:
  - `helm` must be on PATH.
  - Chart dependencies must be present (charts/ directory populated):
      helm dependency build alchemy-observability-agent
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]
CHART_DIR = REPO_ROOT / "alchemy-observability-agent"

_AGENT_PROM_NAME = "shard-name"
_CONFIGMAP_NAME = f"{_AGENT_PROM_NAME}-config-patcher-script"


def _helm_template() -> list[dict[str, Any]]:
    if not (CHART_DIR / "charts").exists():
        pytest.skip(
            "alchemy-observability-agent/charts/ not present — "
            "run: helm dependency build alchemy-observability-agent"
        )

    result = subprocess.run(
        ["helm", "template", "r", str(CHART_DIR), "-f", str(CHART_DIR / "values.yaml")],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\nSTDERR:\n{result.stderr}")

    return [d for d in yaml.safe_load_all(result.stdout) if d is not None]


def _find_prometheus_cr(docs: list[dict[str, Any]]) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == "Prometheus":
            return doc
    kinds = [f"{d.get('kind')}/{d.get('metadata', {}).get('name')}" for d in docs]
    pytest.fail(f"No kind:Prometheus found in rendered output. Kinds present: {kinds}")


def _find_container(containers: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((c for c in containers if c.get("name") == name), None)


def _extract_mutation(container: dict[str, Any]) -> str:
    for env in container.get("env", []):
        if env.get("name") == "MUTATION":
            value = env.get("value")
            if isinstance(value, str) and value:
                return value
    return ""


@pytest.fixture(scope="module")
def base_render() -> list[dict[str, Any]]:
    return _helm_template()


class TestConfigPatcher:
    def test_config_patcher_container_present(self, base_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(base_render)
        names = [c["name"] for c in prom["spec"].get("containers", [])]
        assert "prometheus-config-patcher" in names

    def test_mutation_includes_compaction_threshold(self, base_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(base_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None
        mutation = _extract_mutation(patcher)
        assert "stale_series_compaction_threshold" in mutation

    def test_config_patcher_script_configmap_present(self, base_render: list[dict[str, Any]]) -> None:
        cm = next(
            (
                doc
                for doc in base_render
                if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == _CONFIGMAP_NAME
            ),
            None,
        )
        assert cm is not None
        assert "config-patcher.sh" in cm.get("data", {})

    def test_config_patcher_script_mount_present(self, base_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(base_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None
        mount_names = [m["name"] for m in patcher.get("volumeMounts", [])]
        assert "config-patcher-script" in mount_names
        volume_names = [v["name"] for v in prom["spec"].get("volumes", [])]
        assert "config-patcher-script" in volume_names
