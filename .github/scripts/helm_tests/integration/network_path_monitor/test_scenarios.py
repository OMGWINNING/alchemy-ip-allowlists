"""Assertions over the rendered network-path-monitor scenarios.

Render prerequisite (see helm_tests/helpers/utils.py):
    helm-renderer --target-repo . --target-chart network-path-monitor \\
        --handler .github/scripts/helm_tests/handler.py
"""

from pathlib import Path

from helm_tests.helpers.utils import (
    cronjob_env,
    get_all_resources_of_kind,
    get_resource,
    has_resource,
    load_yaml,
    parametrize_yaml_files,
    probe_static_labels,
    probe_static_targets,
)

CHART = "network-path-monitor"

default_files = parametrize_yaml_files(CHART, ["rendered-default-values.yaml"])
probe_only_files = parametrize_yaml_files(CHART, ["tests/rendered-scenario-probe-only.yaml"])
mtr_probe_files = parametrize_yaml_files(CHART, ["tests/rendered-scenario-mtr-and-probe.yaml"])
mixed_files = parametrize_yaml_files(CHART, ["tests/rendered-scenario-mixed-overrides.yaml"])
icmp_baseline_files = parametrize_yaml_files(CHART, ["tests/rendered-scenario-icmp-baseline.yaml"])
icmp_override_files = parametrize_yaml_files(CHART, ["tests/rendered-scenario-icmp-override.yaml"])


# --- default values: nothing enabled -> only the render-contract ConfigMap ---
@default_files
def test_default_renders_only_render_contract(filepath: Path) -> None:
    docs: list[dict] = load_yaml(filepath)
    assert not has_resource(docs, "Probe")
    assert not has_resource(docs, "CronJob")
    cms: list[dict] = get_all_resources_of_kind(docs, "ConfigMap")
    assert len(cms) == 1
    assert cms[0]["metadata"]["labels"]["app.kubernetes.io/component"] == "render-contract"


# --- probe-only: one Probe, no MTR ---
@probe_only_files
def test_probe_only_shape(filepath: Path) -> None:
    docs: list[dict] = load_yaml(filepath)
    assert len(get_all_resources_of_kind(docs, "Probe")) == 1
    assert not has_resource(docs, "CronJob")
    # no mtr-runner ConfigMap and no render-contract when a target is enabled
    assert not has_resource(docs, "ConfigMap")


@probe_only_files
def test_probe_only_tcp_connect_target_and_labels(filepath: Path) -> None:
    probe: dict = get_resource(load_yaml(filepath), "Probe")
    assert probe["spec"]["module"] == "tcp_connect"
    # tcp_connect target is host:port derived from resolved.*
    assert probe_static_targets(probe) == ["gateway.euc1.example.com:443"]

    labels: dict = probe_static_labels(probe)
    assert labels["vantage_class"] == "production_path"
    assert labels["target_endpoint_role"] == "istioGateway"
    assert labels["target_host"] == "gateway.euc1.example.com"
    assert labels["target_port"] == "443"
    assert labels["target_cluster"] == "archv2-ovh-prod-euc1"
    assert labels["source_cluster"] == "edge-proxy-ovh-prod-apse1"
    assert labels["probe_link"] == (
        "edge-proxy-ovh-prod-apse1__archv2-ovh-prod-euc1__istioGateway__443"
    )


# --- mtr-and-probe: both signals for the same path, shared probe_link ---
@mtr_probe_files
def test_mtr_and_probe_shape(filepath: Path) -> None:
    docs: list[dict] = load_yaml(filepath)
    assert len(get_all_resources_of_kind(docs, "Probe")) == 1
    assert len(get_all_resources_of_kind(docs, "CronJob")) == 1
    # exactly the mtr-runner ConfigMap (no render-contract)
    cms: list[dict] = get_all_resources_of_kind(docs, "ConfigMap")
    assert len(cms) == 1
    assert cms[0]["metadata"]["labels"]["app.kubernetes.io/component"] == "mtr-runner"
    runner: str = cms[0]["data"]["run-mtr.sh"]
    assert "mtr_final_hop_avg_ms" in runner
    assert "mtr_final_hop_loss_pct" in runner
    assert "report.hubs" in runner


@mtr_probe_files
def test_probe_link_parity_between_probe_and_cronjob(filepath: Path) -> None:
    docs: list[dict] = load_yaml(filepath)
    probe_link: str = probe_static_labels(get_resource(docs, "Probe"))["probe_link"]
    env: dict[str, str] = cronjob_env(get_resource(docs, "CronJob"))
    assert env["PROBE_LINK"] == probe_link, "Probe and MTR must share an identical probe_link"


@mtr_probe_files
def test_mtr_host_and_port_derive_from_resolved(filepath: Path) -> None:
    env: dict[str, str] = cronjob_env(get_resource(load_yaml(filepath), "CronJob"))
    assert env["MTR_TARGET_HOST"] == "gateway.euc1.example.com"
    assert env["MTR_PORT"] == "443"


@mtr_probe_files
def test_mtr_security_controls(filepath: Path) -> None:
    cronjob: dict = get_resource(load_yaml(filepath), "CronJob")
    pod_spec: dict = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False

    container: dict = pod_spec["containers"][0]
    sec: dict = container["securityContext"]
    assert sec["readOnlyRootFilesystem"] is True
    assert sec["allowPrivilegeEscalation"] is False
    assert sec["capabilities"]["drop"] == ["ALL"]
    assert sec["capabilities"]["add"] == ["NET_RAW"]
    # enabled MTR must be digest-pinned
    assert "@sha256:" in container["image"]


# --- mixed-overrides: per-target useProbe / mtr.enabled overrides honored ---
@mixed_files
def test_mixed_overrides_split(filepath: Path) -> None:
    docs: list[dict] = load_yaml(filepath)
    probes: list[dict] = get_all_resources_of_kind(docs, "Probe")
    cronjobs: list[dict] = get_all_resources_of_kind(docs, "CronJob")
    # one Probe (http target, mtr disabled) and one CronJob (api target, useProbe false)
    assert len(probes) == 1
    assert len(cronjobs) == 1

    probe_name: str = probe_static_labels(probes[0])["reachability_probe"]
    assert probe_name == "obs-use1-to-apse-ep-http"
    assert probe_static_labels(probes[0])["vantage_class"] == "platform_vantage"

    # http_2xx target string is a full URL built from scheme + host:port + path
    assert probe_static_targets(probes[0]) == ["https://apse-edge-proxy.example.com:443/health"]

    cj_env: dict[str, str] = cronjob_env(cronjobs[0])
    assert cj_env["REACHABILITY_PROBE"] == "apse-ep-to-use1-api"
    assert cj_env["TARGET_ENDPOINT_ROLE"] == "kubernetesApi"
    assert cj_env["MTR_PORT"] == "6443"


# --- icmp-baseline: external ICMP target (Cloudflare 1.1.1.1) ---
@icmp_baseline_files
def test_icmp_baseline_probe_is_host_only(filepath: Path) -> None:
    """The icmp branch renders a bare host (no :port) as the static target."""
    probe: dict = get_resource(load_yaml(filepath), "Probe")
    assert probe["spec"]["module"] == "icmp"
    assert probe_static_targets(probe) == ["1.1.1.1"]


@icmp_baseline_files
def test_icmp_baseline_labels(filepath: Path) -> None:
    """The internet-baseline target carries the right classification labels."""
    labels: dict = probe_static_labels(get_resource(load_yaml(filepath), "Probe"))
    assert labels["target_class"] == "internet_baseline"
    assert labels["target_host"] == "1.1.1.1"
    # Sentinel port appears in labels even though ICMP ignores it.
    assert labels["target_port"] == "443"
    assert labels["target_endpoint_role"] == "external"
    assert labels["source_cluster"] == "edge-proxy-ovh-prod-apse1"
    assert labels["probe_link"] == "edge-proxy-ovh-prod-apse1__cloudflare__external__443"


@icmp_baseline_files
def test_icmp_baseline_mtr_protocol(filepath: Path) -> None:
    """The MTR CronJob uses the icmp protocol and shares the probe_link."""
    docs: list[dict] = load_yaml(filepath)
    cronjob: dict = get_resource(docs, "CronJob")
    env: dict[str, str] = cronjob_env(cronjob)
    assert env["MTR_PROTOCOL"] == "icmp"
    assert env["MTR_TARGET_HOST"] == "1.1.1.1"
    # Probe and CronJob share an identical probe_link.
    probe_link: str = probe_static_labels(get_resource(docs, "Probe"))["probe_link"]
    assert env["PROBE_LINK"] == probe_link


@icmp_baseline_files
def test_icmp_baseline_digest_pinned(filepath: Path) -> None:
    """Enabled MTR must be digest-pinned."""
    container: dict = get_resource(load_yaml(filepath), "CronJob")["spec"]["jobTemplate"]["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert "@sha256:" in container["image"]


# --- icmp-override: explicit probe.target wins over the icmp branch ---
@icmp_override_files
def test_icmp_override_target_wins(filepath: Path) -> None:
    """The explicit probe.target override is emitted verbatim, not the host."""
    probe: dict = get_resource(load_yaml(filepath), "Probe")
    assert probe["spec"]["module"] == "icmp"
    assert probe_static_targets(probe) == ["1.0.0.1"]
