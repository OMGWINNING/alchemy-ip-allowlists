"""Regression tests for rollout preflight behavior."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from rollout import create_update_prs


def test_stage_preflight_authenticates_once_and_checks_every_requested_chart():
    creator = create_update_prs.PRCreator(dry_run=True)
    creator.chart_versions = {"core": "0.0.64", "shard": "0.0.42"}
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with patch.dict(os.environ, {"GHCR_TOKEN": "test-token"}), patch.object(
        create_update_prs.subprocess, "run", side_effect=fake_run
    ):
        creator.preflight_chart_publication(["core", "shard"])

    assert calls == [
        ["helm", "registry", "login", "-u", "username", "--password-stdin", "ghcr.io"],
        [
            "helm",
            "show",
            "chart",
            "oci://ghcr.io/alchemy-docker/helm-charts/alchemy-observability-core",
            "--version",
            "0.0.64",
        ],
        [
            "helm",
            "show",
            "chart",
            "oci://ghcr.io/alchemy-docker/helm-charts/alchemy-observability-shard",
            "--version",
            "0.0.42",
        ],
    ]


def test_stage_preflight_fails_before_subprocess_when_ghcr_token_is_missing():
    creator = create_update_prs.PRCreator(dry_run=True)
    creator.chart_versions = {"core": "0.0.64"}

    with patch.dict(os.environ, {"GHCR_TOKEN": ""}), patch.object(
        create_update_prs.subprocess, "run"
    ) as run, pytest.raises(SystemExit, match="1"):
        creator.preflight_chart_publication(["core"])

    run.assert_not_called()


def test_config_rollout_does_not_generate_ignored_values_files(tmp_path):
    config_file = (
        tmp_path
        / "helm/observability-platform/stage/use1/aws/example/config.yaml"
    )
    config_file.parent.mkdir(parents=True)
    config_file.write_text("chartVersion: 0.0.41\n")

    creator = create_update_prs.PRCreator()
    creator.chart_versions = {"shard": "0.0.42"}

    with patch.object(create_update_prs, "REPO_ROOT", tmp_path), patch.object(
        creator, "run_cmd"
    ) as run_cmd:
        creator.update_config_files("stage", "use1", ["shard"])

    assert config_file.read_text() == "chartVersion: 0.0.42\n"
    run_cmd.assert_not_called()
