#!/usr/bin/env python3
"""Repo-local helm-renderer handler for observability-infra-charts.

Extends the packaged ``ObservabilityInfraChartsHandler`` so the standard
``--generate-values`` / values-first rendering is preserved unchanged for every
chart in the repo, while adding override-style render *scenarios* for charts in
``SCENARIO_CHARTS`` (currently ``network-path-monitor``).

Following the pattern used across the infra-charts repos (cf. the alchemy-base
values generator), the scenario values are **generated** at render time and
written to a gitignored ``<chart>/test-values-files/`` directory rather than
committed. Each scenario is rendered as ``[<chart>/values.yaml, <generated>]``
-- base first, scenario overlay LAST -- so the fixture overrides the shipped
defaults. Output lands under
``<chart>/helm-render-output/tests/rendered-scenario-<name>.yaml``, which the
pytest suite in ``.github/scripts/helm_tests/`` asserts against.

Negative (expected-fail) scenarios are NOT rendered here -- they are exercised
directly by ``helm template`` in the pytest suite, which imports
``NEGATIVE_SCENARIOS`` from this module.

Select this handler explicitly:

    uv run --project .github/scripts helm-renderer \\
        --target-repo . --target-chart network-path-monitor \\
        --handler .github/scripts/helm_tests/handler.py
"""

import pathlib

import yaml
from loguru import logger

from helm_renderer.repo_handlers.base import TestCase
from helm_renderer.repo_handlers.observability_infra_charts import (
    ObservabilityInfraChartsHandler,
)

# Charts that carry generated scenario overlays.
SCENARIO_CHARTS: set[str] = {"network-path-monitor"}

# Directory (under the chart, gitignored) that generated fake values are written to.
FAKE_VALUES_DIR: str = "test-values-files"

_DIGEST_A: str = "sha256:" + "a" * 64
_DIGEST_B: str = "sha256:" + "b" * 64

_TARGET_EUC1_GATEWAY: dict = {
    "name": "apse-ep-to-euc1-archv2",
    "enabled": True,
    "clusterRef": "prod/euc1/ovh/archv2",
    "endpoint": "auto",
    "resolved": {
        "host": "gateway.euc1.example.com",
        "port": 443,
        "endpointRole": "istioGateway",
        "targetRegion": "euc1",
        "targetProvider": "ovh",
        "targetCluster": "archv2-ovh-prod-euc1",
    },
    "direction": "egress",
    "checkType": "egress",
    "targetClass": "edge_proxy",
}

# An external (non-clusterRef) ICMP internet-baseline target — the Cloudflare
# 1.1.1.1 control. Tests the icmp probe branch (host-only target string) and
# the icmp MTR protocol. Sentinels: port 443 (ICMP ignores it; required by the
# templates and validatePositiveInt), endpointRole: external.
_TARGET_CLOUDFLARE_BASELINE: dict = {
    "name": "cloudflare-1111",
    "enabled": True,
    "resolved": {
        "host": "1.1.1.1",
        "port": 443,
        "endpointRole": "external",
        "targetRegion": "global",
        "targetProvider": "cloudflare",
        "targetCluster": "cloudflare",
    },
    "direction": "egress",
    "checkType": "egress",
    "targetClass": "internet_baseline",
    "probe": {"module": "icmp"},
    "mtr": {"enabled": True, "protocol": "icmp"},
}

# Positive scenarios: rendered via the helm-renderer, asserted by test_scenarios.py.
POSITIVE_SCENARIOS: dict[str, dict] = {
    # One enabled target, MTR left disabled -> a single Probe.
    "probe-only": {
        "targets": [_TARGET_EUC1_GATEWAY],
    },
    # MTR + Probe for the same target -> both signals, shared probe_link.
    "mtr-and-probe": {
        "mtr": {"enabled": True, "image": {"digest": _DIGEST_A}},
        "targets": [_TARGET_EUC1_GATEWAY],
    },
    # ICMP internet-baseline (Cloudflare 1.1.1.1): Probe + MTR, both icmp.
    "icmp-baseline": {
        "mtr": {"enabled": True, "image": {"digest": _DIGEST_A}},
        "targets": [_TARGET_CLOUDFLARE_BASELINE],
    },
    # ICMP with an explicit probe.target override — the escape hatch at the
    # top of probeTarget must win over the icmp branch.
    "icmp-override": {
        "targets": [
            {
                **_TARGET_CLOUDFLARE_BASELINE,
                "name": "icmp-override",
                "probe": {"module": "icmp", "target": "1.0.0.1"},
            },
        ],
    },
    # Per-target overrides: target A Probe-only (http_2xx, platform_vantage);
    # target B MTR-only (useProbe: false, kubernetesApi endpoint).
    "mixed-overrides": {
        "useProbes": True,
        "mtr": {"enabled": True, "image": {"digest": _DIGEST_B}},
        "targets": [
            {
                "name": "obs-use1-to-apse-ep-http",
                "enabled": True,
                "clusterRef": "prod/apse1/ovh/edge-proxy",
                "endpoint": "istioGateway",
                "resolved": {
                    "host": "apse-edge-proxy.example.com",
                    "port": 443,
                    "endpointRole": "istioGateway",
                    "targetRegion": "apse1",
                    "targetProvider": "ovh",
                    "targetCluster": "edge-proxy-ovh-prod-apse1",
                },
                "direction": "ingress",
                "checkType": "ingress",
                "targetClass": "edge_proxy",
                "sourceRegion": "use1",
                "sourceProvider": "aws",
                "sourceEnvironment": "prod",
                "sourceCluster": "observability-aws-ops-use1-0",
                "vantageClass": "platform_vantage",
                "probe": {"module": "http_2xx", "scheme": "https", "path": "/health"},
                "mtr": {"enabled": False},
            },
            {
                "name": "apse-ep-to-use1-api",
                "enabled": True,
                "clusterRef": "prod/use1/ovh/legacy-cluster",
                "endpoint": "kubernetesApi",
                "resolved": {
                    "host": "api.use1-legacy.example.com",
                    "port": 6443,
                    "endpointRole": "kubernetesApi",
                    "targetRegion": "use1",
                    "targetProvider": "ovh",
                    "targetCluster": "legacy-cluster-ovh-prod-use1",
                },
                "direction": "egress",
                "checkType": "mtr",
                "targetClass": "kubernetes_api",
                "useProbe": False,
                "mtr": {"protocol": "tcp"},
            },
        ],
    },
}

# Negative scenarios: expected to FAIL rendering. Consumed by test_negative.py,
# which writes them to a temp file and asserts `helm template` is rejected.
NEGATIVE_SCENARIOS: dict[str, dict] = {
    # http_* module without probe.scheme/probe.path.
    "http-missing-scheme": {
        "targets": [
            {
                **_TARGET_EUC1_GATEWAY,
                "name": "bad-http-target",
                "probe": {"module": "http_2xx"},
            }
        ],
    },
    # MTR enabled without a sha256 image digest.
    "mtr-missing-digest": {
        "mtr": {"enabled": True, "image": {"digest": ""}},
        "targets": [{**_TARGET_EUC1_GATEWAY, "name": "needs-digest"}],
    },
    # Two enabled targets that collapse to the same Probe metadata.name.
    "duplicate-name": {
        "targets": [
            {**_TARGET_EUC1_GATEWAY, "name": "dup-target"},
            {
                **_TARGET_EUC1_GATEWAY,
                "name": "dup-target",
                "clusterRef": "prod/usw2/ovh/archv2",
                "resolved": {
                    "host": "gateway.usw2.example.com",
                    "port": 443,
                    "endpointRole": "istioGateway",
                    "targetRegion": "usw2",
                    "targetProvider": "ovh",
                    "targetCluster": "archv2-ovh-prod-usw2",
                },
            },
        ],
    },
    # Unknown module (not tcp_connect / http_* / icmp) without an explicit
    # probe.target — the helper must still fail-closed.
    "unknown-module": {
        "targets": [
            {**_TARGET_EUC1_GATEWAY, "name": "bad-dns-target", "probe": {"module": "dns"}},
        ],
    },
}


def write_fake_values(chart_dir: str, scenarios: dict[str, dict]) -> dict[str, str]:
    """Write each scenario dict to a gitignored fake-values file under the chart.

    Args:
        chart_dir: Directory of the chart.
        scenarios: Mapping of scenario name -> Helm values overlay.

    Returns:
        Mapping of scenario name -> generated file path.
    """
    out_dir: pathlib.Path = pathlib.Path(chart_dir) / FAKE_VALUES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale files first so a renamed/removed scenario doesn't linger in
    # the (gitignored) generated dir and confuse anyone inspecting it.
    for stale in out_dir.glob("fake-values-*.yaml"):
        stale.unlink()

    generated: dict[str, str] = {}
    for name, values in scenarios.items():
        path: pathlib.Path = out_dir / f"fake-values-{name}.yaml"
        with path.open("w") as f:
            yaml.safe_dump(values, f, default_flow_style=False, sort_keys=False)
        generated[name] = str(path)
    logger.info(f"Generated {len(generated)} fake values file(s) in {out_dir}")
    return generated


class ObservabilityInfraChartsScenarioHandler(ObservabilityInfraChartsHandler):
    """Observability handler plus generated scenario rendering for opted-in charts."""

    def pre_render(self, chart_dir: str) -> dict[str, str]:
        """Generate the positive scenario fake values for opted-in charts."""
        if pathlib.Path(chart_dir).name not in SCENARIO_CHARTS:
            return {}
        return write_fake_values(chart_dir, POSITIVE_SCENARIOS)

    def get_test_cases(self, chart_dir: str, values_files_map: dict[str, str]) -> list[TestCase]:
        """Standard cases for every chart, plus a case per generated scenario."""
        cases: list[TestCase] = super().get_test_cases(chart_dir, values_files_map)
        if pathlib.Path(chart_dir).name not in SCENARIO_CHARTS or not values_files_map:
            return cases

        base: str = str(pathlib.Path(chart_dir) / "values.yaml")
        for name in sorted(values_files_map):
            cases.append(
                TestCase(
                    name=f"Render {chart_dir} scenario {name}",
                    description=f"Rendering {chart_dir} with values.yaml + generated {name}",
                    identifier=f"scenario-{name}",
                    values_files=[base, values_files_map[name]],
                    subdirectory="tests",
                )
            )
        return cases
