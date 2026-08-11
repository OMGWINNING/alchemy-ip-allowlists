"""End-to-end coverage for flattened MTR summary fields."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq is required by the MTR runner"
)

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNNER = REPO_ROOT / "network-path-monitor" / "files" / "run-mtr.sh"
FIXTURE = Path(__file__).parent / "fixtures" / "mtr-report.json"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_runner_flattens_final_hop_summary(tmp_path: Path) -> None:
    """A real-shaped mtr --json report yields dashboard-ready final-hop fields."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "mtr",
        '#!/bin/sh\nif [ "$1" = "--help" ]; then echo --json; exit 0; fi\ncat "$MTR_FIXTURE"\n',
    )
    _write_executable(fake_bin / "timeout", '#!/bin/sh\nshift\nexec "$@"\n')
    _write_executable(fake_bin / "getent", '#!/bin/sh\necho "203.0.113.10 STREAM target.example"\n')

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MTR_FIXTURE": str(FIXTURE),
        "MTR_TARGET_HOST": "target.example",
        "MTR_PROTOCOL": "tcp",
        "MTR_PORT": "443",
        "MTR_REPORT_CYCLES": "10",
        "MTR_TIMEOUT_SECONDS": "120",
        "REACHABILITY_PROBE": "target-gateway",
        "PROBE_LINK": "source__target__istioGateway__443",
        "DIRECTION": "egress",
        "CHECK_TYPE": "egress",
        "VANTAGE_CLASS": "production_path",
        "SOURCE_REGION": "use1",
        "SOURCE_PROVIDER": "ovh",
        "SOURCE_VRACK": "",
        "SOURCE_ENVIRONMENT": "prod",
        "SOURCE_CLUSTER": "edge-proxy-ovh-prod-use1",
        "TARGET_CLASS": "archv2_gateway",
        "TARGET_REGION": "euc1",
        "TARGET_PROVIDER": "ovh",
        "TARGET_CLUSTER": "archv2-prod-euc1",
        "TARGET_ENDPOINT_ROLE": "istioGateway",
    }

    result = subprocess.run(
        ["sh", str(RUNNER)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    artifact = json.loads(result.stdout)

    assert artifact["mtr_hop_count"] == 2
    assert artifact["mtr_final_hop_host"] == "target-gateway"
    assert artifact["mtr_final_hop_loss_pct"] == 2.5
    assert artifact["mtr_final_hop_avg_ms"] == 42.7
    assert artifact["mtr_final_hop_worst_ms"] == 51.2
    assert artifact["mtr_final_hop_stdev_ms"] == 3.4
