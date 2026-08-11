"""Shared helpers for reading helm-renderer output in pytest.

Tests assert against the YAML the helm-renderer writes to
``<chart>/helm-render-output/`` (see .github/scripts/helm_tests/handler.py). Render first:

    uv run --project .github/scripts helm-renderer --target-repo . \\
        --target-chart network-path-monitor --handler .github/scripts/helm_tests/handler.py
"""

from pathlib import Path

import pytest
import yaml
from loguru import logger


def find_project_root(chart_name: str) -> Path:
    """Walk up from this file to the first ancestor containing ``chart_name``.

    Args:
        chart_name: Chart directory name to locate (e.g. "network-path-monitor").

    Returns:
        The repository root that contains the chart, or a sensible fallback.
    """
    current: Path = Path(__file__).resolve()
    for parent in [current, *current.parents]:
        if (parent / chart_name).exists():
            return parent
    logger.warning(f"Chart {chart_name} not found walking up from {current}; using fallback")
    return current.parents[3]


def get_yaml_files(chart_name: str, patterns: list[str]) -> list[Path]:
    """Return rendered YAML files under the chart's helm-render-output dir.

    Args:
        chart_name: Chart directory name.
        patterns: Glob patterns relative to ``<chart>/helm-render-output``.

    Returns:
        Sorted, de-duplicated list of matching file paths.
    """
    output_dir: Path = find_project_root(chart_name) / chart_name / "helm-render-output"
    files: list[Path] = []
    for pattern in patterns:
        files.extend(output_dir.glob(pattern))
    return sorted(set(files))


def load_yaml(filepath: Path) -> list[dict]:
    """Load a rendered file, returning only top-level K8s manifests."""
    manifests: list[dict] = []
    with open(filepath) as f:
        for doc in yaml.safe_load_all(f):
            if isinstance(doc, dict) and "kind" in doc and "apiVersion" in doc:
                manifests.append(doc)
    return manifests


def has_resource(docs: list[dict], resource_kind: str) -> bool:
    """True if at least one manifest of ``resource_kind`` is present."""
    return any(doc.get("kind") == resource_kind for doc in docs)


def get_all_resources_of_kind(docs: list[dict], resource_kind: str) -> list[dict]:
    """Return every manifest matching ``resource_kind``."""
    return [doc for doc in docs if isinstance(doc, dict) and doc.get("kind") == resource_kind]


def get_resource(docs: list[dict], resource_kind: str, index: int = 0) -> dict:
    """Return the Nth manifest of a given kind.

    Raises:
        ValueError: if no manifest of that kind exists.
        IndexError: if ``index`` is out of range.
    """
    matches: list[dict] = get_all_resources_of_kind(docs, resource_kind)
    if not matches:
        raise ValueError(f"No resource found with kind: {resource_kind}")
    return matches[index]


def probe_static_labels(probe: dict) -> dict:
    """Return the staticConfig metric labels attached to a Probe's targets."""
    return probe["spec"]["targets"]["staticConfig"]["labels"]


def probe_static_targets(probe: dict) -> list[str]:
    """Return the static target strings of a Probe."""
    return probe["spec"]["targets"]["staticConfig"]["static"]


def cronjob_env(cronjob: dict) -> dict[str, str]:
    """Return the literal (name -> value) env pairs of a CronJob's mtr container.

    Skips env entries sourced from valueFrom (pod field refs).
    """
    containers: list[dict] = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ]
    env: dict[str, str] = {}
    for entry in containers[0].get("env", []):
        if "value" in entry:
            env[entry["name"]] = entry["value"]
    return env


def parametrize_yaml_files(chart_name: str, patterns: list[str]) -> pytest.MarkDecorator:
    """Parametrize a test over rendered YAML files, or skip if none exist.

    A skip signals the helm-renderer step did not produce the expected output;
    ``PYTEST_FAIL_ON_SKIP=1`` turns that into a failure.

    Args:
        chart_name: Chart directory name.
        patterns: Glob patterns relative to ``<chart>/helm-render-output``.

    Returns:
        A ``pytest.mark.parametrize`` decorator over ``filepath``.
    """
    files: list[Path] = get_yaml_files(chart_name, patterns)
    if not files:
        return pytest.mark.skip(f"No rendered files for {chart_name} matching {patterns}")
    root: Path = find_project_root(chart_name)
    return pytest.mark.parametrize("filepath", files, ids=lambda p: str(p.relative_to(root)))
