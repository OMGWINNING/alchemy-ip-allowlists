"""Fail-closed tests for network-path-monitor.

These scenarios are EXPECTED to fail rendering, so they cannot go through the
helm-renderer output path (which asserts success). Each ``NEGATIVE_SCENARIOS``
overlay (defined in the repo-local handler) is written to a temp file and fed to
``helm template`` directly; the render must be rejected with the intended
validation message.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from helm_tests import handler
from helm_tests.helpers.utils import find_project_root

CHART = "network-path-monitor"
CHART_DIR: Path = find_project_root(CHART) / CHART

# scenario name -> substring expected in the helm error output
EXPECTED_ERRORS: dict[str, str] = {
    "http-missing-scheme": "probe.scheme is required",
    "mtr-missing-digest": "mtr.image.digest must be a sha256 digest",
    "duplicate-name": "must be unique after RFC 1123 sanitization",
    "unknown-module": "probe.target must be set explicitly",
}


@pytest.mark.parametrize("scenario", list(EXPECTED_ERRORS), ids=list(EXPECTED_ERRORS))
def test_negative_scenario_fails_closed(scenario: str, tmp_path: Path) -> None:
    assert scenario in handler.NEGATIVE_SCENARIOS, f"unknown negative scenario: {scenario}"

    overlay: Path = tmp_path / f"fake-values-{scenario}.yaml"
    overlay.write_text(yaml.safe_dump(handler.NEGATIVE_SCENARIOS[scenario], sort_keys=False))

    result = subprocess.run(
        [
            "helm",
            "template",
            str(CHART_DIR),
            "-f",
            str(CHART_DIR / "values.yaml"),
            "-f",
            str(overlay),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, f"{scenario} rendered successfully but should have failed closed"
    combined: str = result.stdout + result.stderr
    expected: str = EXPECTED_ERRORS[scenario]
    assert expected in combined, (
        f"{scenario} failed, but without the expected message {expected!r}.\n{combined}"
    )
