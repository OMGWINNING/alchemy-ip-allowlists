"""
Golden-render tests for alchemy-observability-shard patch variants.

Prerequisites:
  - `helm` must be on PATH.
  - Chart dependencies must be present (charts/ directory populated):
      helm dependency build alchemy-observability-shard
    CI caches charts/ between runs; run once locally after a fresh clone.

These tests guard against three classes of regression:
  1. A patch edit that silently loses the prometheus-config-patcher sidecar
     (which is an easy mistake because prometheusSpec.containers is a
     full-list Helm override — there is no by-name merge).
  2. The ECS overlay dropping auth-proxy (same full-list override — routes
     to :8081 return 503 with nothing listening).
  3. A hardcoded retentionSize sneaking into a patch (size-based retention
     is managed at runtime by the prometheus-config-patcher sidecar via
     storage.tsdb.retention.percentage; a static retentionSize would fight
     it and cause hard-to-diagnose TSDB compaction behaviour).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]
CHART_DIR = REPO_ROOT / "alchemy-observability-shard"
PATCHES_DIR = CHART_DIR / "patches"

_SHARD_NAME = "test-shard"

# Substitute values for every ${VAR} placeholder that appears across the patch
# files.  These are the same kinds of values the helm-renderer CMP bakes in at
# ArgoCD sync time; exact numbers are unimportant for these structural tests.
_SHARD_VARS: dict[str, str] = {
    "SHARD_NAME": _SHARD_NAME,
    "SHARD_RETENTION": "30d",
    "REGION": "use1",
    "CLUSTER": "test-cluster",
    "PROVIDER": "aws",
    "ENV": "stage",
    "SOURCE_CLUSTER": "test-source-cluster",
    "CPU_REQUEST": "100m",
    "CPU_LIMIT": "4000m",
    "MEMORY_REQUEST": "3072Mi",
    "MEMORY_LIMIT": "8192Mi",
    "THANOS_MEMORY_REQUEST": "1024Mi",
    "THANOS_MEMORY_LIMIT": "4096Mi",
    "STORAGE": "50Gi",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _substitute_vars(text: str) -> str:
    for key, value in _SHARD_VARS.items():
        text = text.replace(f"${{{key}}}", value)
    return text


def _flatten_shard_alias(raw: dict[str, Any]) -> dict[str, Any]:
    """Promote the shard-aliased block to the top level.

    patches/values.yaml uses ``${SHARD_NAME}`` as a Helm alias key so the
    values wire up to the kube-prometheus-stack subchart via the alias set in
    the generated per-shard Chart.yaml.  When running helm template against the
    source chart (which has no alias), we flatten that extra nesting so the
    values reach ``kube-prometheus-stack.*`` directly.
    """
    shard_block = raw.pop(_SHARD_NAME, {})
    raw.update(shard_block)
    return raw


def _load_patch_values(path: Path) -> dict[str, Any]:
    content = _substitute_vars(path.read_text())
    return _flatten_shard_alias(yaml.safe_load(content))


def _load_ecs_patch_values() -> dict[str, Any]:
    """Extract the embedded values from the ECS discovery ConfigMap wrapper."""
    content = _substitute_vars((PATCHES_DIR / "ecs-discovery.yaml").read_text())
    cm = yaml.safe_load(content)
    embedded_yaml: str = cm["data"]["values.yaml"]
    return _flatten_shard_alias(yaml.safe_load(embedded_yaml))


def _helm_template(*extra_values: dict[str, Any]) -> list[dict[str, Any]]:
    """Run ``helm template`` against the shard chart with optional extra values.

    The chart's own values.yaml is always applied first; each entry in
    *extra_values* is written to a temp file and appended as a ``-f`` flag
    (last file wins for scalar / list collisions, matching Helm's semantics).
    """
    if not (CHART_DIR / "charts").exists():
        pytest.skip(
            "alchemy-observability-shard/charts/ not present — "
            "run: helm dependency build alchemy-observability-shard"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        value_files: list[str] = ["-f", str(CHART_DIR / "values.yaml")]
        for i, vals in enumerate(extra_values):
            vf = tmp / f"extra-{i}.yaml"
            vf.write_text(yaml.dump(vals, default_flow_style=False))
            value_files += ["-f", str(vf)]

        result = subprocess.run(
            ["helm", "template", "r", str(CHART_DIR), *value_files],
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


def _find_alert(docs: list[dict[str, Any]], alert_name: str) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") != "PrometheusRule":
            continue
        for group in doc.get("spec", {}).get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("alert") == alert_name:
                    return rule
    pytest.fail(f"No alert named {alert_name!r} in rendered PrometheusRule resources")


def _write_relabel_configs(prom: dict[str, Any]) -> list[dict[str, Any]]:
    return prom["spec"]["remoteWrite"][0]["writeRelabelConfigs"]


def _extract_mutation(docs: list[dict[str, Any]], container: dict[str, Any]) -> str:
    """Return the MUTATION yq expression for the config-patcher sidecar.

    MUTATION is passed via container env (see patches/values.yaml). Inline
    args and ConfigMap script-body fallbacks remain for older renders.
    """
    for env in container.get("env", []):
        if env.get("name") == "MUTATION":
            value = env.get("value")
            if isinstance(value, str) and value:
                return value
    for arg in container.get("args", []):
        if not isinstance(arg, str):
            continue
        m = re.search(r"MUTATION='([^']+)'", arg)
        if m:
            return m.group(1)
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for value in doc.get("data", {}).values():
            if not isinstance(value, str):
                continue
            m = re.search(r"MUTATION='([^']+)'", value)
            if m:
                return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Shared fixtures (module-scoped to run helm template once per variant)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_render() -> list[dict[str, Any]]:
    """Manifests rendered from the shard chart's checked-in defaults."""
    return _helm_template()


@pytest.fixture(scope="module")
def standard_render() -> list[dict[str, Any]]:
    """Manifests rendered with only the standard (non-ECS) shard patch."""
    return _helm_template(_load_patch_values(PATCHES_DIR / "values.yaml"))


@pytest.fixture(scope="module")
def stage_render() -> list[dict[str, Any]]:
    """Manifests rendered with the standard and stage remote-write patches."""
    standard = _load_patch_values(PATCHES_DIR / "values.yaml")
    content = _substitute_vars((PATCHES_DIR / "stage-remote-write.yaml").read_text())
    config_map = yaml.safe_load(content)
    stage = _flatten_shard_alias(yaml.safe_load(config_map["data"]["values.yaml"]))
    return _helm_template(standard, stage)


@pytest.fixture(scope="module")
def ecs_render() -> list[dict[str, Any]]:
    """Manifests rendered with the standard patch *and* the ECS overlay.

    The ECS patch overrides prometheusSpec.containers wholesale, which is the
    exact scenario that caused the SEV2: the patcher was lost when ecs-discovery
    replaced the containers list without re-including it.
    """
    return _helm_template(
        _load_patch_values(PATCHES_DIR / "values.yaml"),
        _load_ecs_patch_values(),
    )


@pytest.fixture(scope="module")
def euc1_render() -> list[dict[str, Any]]:
    """Manifests rendered with the Euc1 regional write host enabled."""
    base = _load_patch_values(PATCHES_DIR / "values.yaml")
    base.setdefault("shard", {})["region"] = "euc1"
    base.setdefault("authProxy", {})["regionalWriteHost"] = True
    return _helm_template(base)


# ---------------------------------------------------------------------------
# Standard variant assertions
# ---------------------------------------------------------------------------


class TestBaseValues:
    def test_default_tenant_external_label(self, base_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(base_render)
        assert prom["spec"]["externalLabels"] == {"tenant_id": "default-tenant"}

    def test_remote_write_drops_tenant_after_long_term_keep(
        self, base_render: list[dict[str, Any]]
    ) -> None:
        prom = _find_prometheus_cr(base_render)
        assert _write_relabel_configs(prom) == [
            {"sourceLabels": ["longTerm"], "regex": "true", "action": "keep"},
            {"action": "labeldrop", "regex": "tenant_id"},
        ]


class TestStandardPatch:
    def test_config_patcher_container_present(self, standard_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(standard_render)
        names = [c["name"] for c in prom["spec"].get("containers", [])]
        assert "prometheus-config-patcher" in names, (
            f"prometheus-config-patcher missing from prometheusSpec.containers; found: {names}"
        )

    def test_retention_time_env_present(self, standard_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(standard_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found"
        env_names = [e["name"] for e in patcher.get("env", [])]
        assert "RETENTION_TIME" in env_names, f"RETENTION_TIME env var missing; got: {env_names}"

    def test_mutation_includes_retention_percentage(
        self, standard_render: list[dict[str, Any]]
    ) -> None:
        prom = _find_prometheus_cr(standard_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found"
        mutation = _extract_mutation(standard_render, patcher)
        assert "retention.percentage = 80" in mutation, (
            f"retention.percentage = 80 not in MUTATION; got: {mutation!r}"
        )

    def test_mutation_excludes_stale_series_threshold(
        self, standard_render: list[dict[str, Any]]
    ) -> None:
        prom = _find_prometheus_cr(standard_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found"
        mutation = _extract_mutation(standard_render, patcher)
        assert mutation, "MUTATION could not be located in any rendered ConfigMap"
        assert "stale_series_compaction_threshold" not in mutation, (
            f"stale_series_compaction_threshold must not be in MUTATION; got: {mutation!r}"
        )

    def test_no_hardcoded_retention_size(self, standard_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(standard_render)
        assert "retentionSize" not in prom["spec"], (
            "prometheusSpec.retentionSize must not be set — size-based retention "
            "is managed by the prometheus-config-patcher sidecar at runtime"
        )

    def test_default_tenant_external_label(self, standard_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(standard_render)
        assert prom["spec"]["externalLabels"] == {"tenant_id": "default-tenant"}

    def test_remote_write_drops_tenant_after_long_term_keep(
        self, standard_render: list[dict[str, Any]]
    ) -> None:
        prom = _find_prometheus_cr(standard_render)
        assert _write_relabel_configs(prom) == [
            {"sourceLabels": ["longTerm"], "regex": "true", "action": "keep"},
            {"action": "labeldrop", "regex": "tenant_id"},
        ]

    def test_product_external_label_preserves_tenant(self) -> None:
        base = _load_patch_values(PATCHES_DIR / "values.yaml")
        prom_spec = base["kube-prometheus-stack"]["prometheus"]["prometheusSpec"]
        # Mirror helm-renderer's upstream generate-values merge; that behavior is
        # covered in cloud-infra-tools, while this test covers Helm passthrough.
        prom_spec.setdefault("externalLabels", {})["product"] = "archv2"
        render = _helm_template(base)
        prom = _find_prometheus_cr(render)
        assert prom["spec"]["externalLabels"] == {
            "tenant_id": "default-tenant",
            "product": "archv2",
        }

    def test_remote_source_alert_keys_off_heartbeat_not_up(
        self, standard_render: list[dict[str, Any]]
    ) -> None:
        """RemotePrometheusInstancesDown must stay a "went quiet" heartbeat rule.

        An absence-based form (absent/absent_over_time) pages forever on the
        platform shards that have no remote core/agent producer, so guard
        against a regression back to one.
        """
        rule = _find_alert(standard_render, "RemotePrometheusInstancesDown")
        expr = rule["expr"]

        assert "count_over_time(remote_source_heartbeat[24h])" in expr
        assert "count_over_time(remote_source_heartbeat[10m])" in expr
        assert "unless" in expr
        assert "count by (cluster, environment)" in expr
        assert "absent" not in expr
        assert "up{" not in expr
        assert "process_start_time_seconds" not in expr

        assert rule["for"] == "15m"
        assert rule["labels"]["severity"] == "critical"
        assert rule["labels"]["shard"] == _SHARD_NAME
        assert rule["labels"]["region"] == "use1"
        # `cluster` identifies the offending producer and must come from the
        # query result, not be pinned to a render-time value.
        assert "cluster" not in rule["labels"]
        assert "$labels.cluster" in rule["annotations"]["description"]


class TestStagePatch:
    def test_remote_write_drops_tenant_before_operational_labels(
        self, stage_render: list[dict[str, Any]]
    ) -> None:
        prom = _find_prometheus_cr(stage_render)
        assert _write_relabel_configs(prom) == [
            {"sourceLabels": ["longTerm"], "regex": "true", "action": "keep"},
            {"action": "labeldrop", "regex": "tenant_id"},
            {
                "action": "labeldrop",
                "regex": "instance|pod|container|endpoint|service|namespace|job",
            },
        ]


class TestEuc1ObsPatch:
    def test_regional_write_host_on_virtualservice(self, euc1_render: list[dict[str, Any]]) -> None:
        vs = next(
            m
            for m in euc1_render
            if m.get("kind") == "VirtualService" and m["metadata"]["name"] == _SHARD_NAME
        )
        hosts = vs["spec"]["hosts"]
        assert f"{_SHARD_NAME}.euc1.rm.obs-public.i.alchemy.com" in hosts


# ---------------------------------------------------------------------------
# VPA variant assertions
# ---------------------------------------------------------------------------


def _find_vpa(docs: list[dict[str, Any]]) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == "VerticalPodAutoscaler":
            return doc
    kinds = [f"{d.get('kind')}/{d.get('metadata', {}).get('name')}" for d in docs]
    pytest.fail(f"No VerticalPodAutoscaler found in rendered output. Kinds present: {kinds}")


def _render_with_vpa_patch(patch_name: str) -> list[dict[str, Any]]:
    base = _load_patch_values(PATCHES_DIR / "values.yaml")
    content = _substitute_vars((PATCHES_DIR / patch_name).read_text())
    config_map = yaml.safe_load(content)
    overlay = _flatten_shard_alias(yaml.safe_load(config_map["data"]["values.yaml"]))
    return _helm_template(base, overlay)


class TestVpa:
    """Guards for templates/vpa.yaml and the vpa-* patch ladder."""

    def test_vpa_not_rendered_by_default(self, base_render: list[dict[str, Any]]) -> None:
        """VPA is opt-in: clusters without the VPA CRD must not get an object."""
        assert all(doc.get("kind") != "VerticalPodAutoscaler" for doc in base_render)

    def test_vpa_base_patch_targets_prometheus_cr(self) -> None:
        """targetRef must be the Prometheus CR, never the StatefulSet.

        Since operator 0.71 the Prometheus CRD exposes /scale and VPA rejects a
        targetRef whose owner has a scale subresource (prometheus-operator#6291).
        """
        vpa = _find_vpa(_render_with_vpa_patch("vpa-base.yaml"))
        assert vpa["spec"]["targetRef"] == {
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "Prometheus",
            "name": _SHARD_NAME,
        }
        assert vpa["spec"]["updatePolicy"]["updateMode"] == "Initial"
        assert vpa["spec"]["updatePolicy"]["minReplicas"] == 2

    def test_sidecars_pinned_out_of_default_policy(self) -> None:
        """Unlisted containers inherit the default policy and would get resized
        once updateMode moves past Off — every sidecar must be pinned Off."""
        vpa = _find_vpa(_render_with_vpa_patch("vpa-base.yaml"))
        policies = {
            p["containerName"]: p for p in vpa["spec"]["resourcePolicy"]["containerPolicies"]
        }
        assert policies["prometheus"].get("mode", "Auto") == "Auto"
        assert policies["prometheus"]["controlledValues"] == "RequestsAndLimits"
        for sidecar in (
            "config-reloader",
            "thanos-sidecar",
            "istio-proxy",
            "prometheus-config-patcher",
            "ecs-target-sync",
        ):
            assert policies[sidecar]["mode"] == "Off", f"{sidecar} must be pinned to mode Off"

    @pytest.mark.parametrize(
        ("patch_name", "expected_mode"),
        [
            ("vpa-base.yaml", "Initial"),
            ("vpa-active.yaml", "Recreate"),
            ("vpa-active-inplace.yaml", "InPlaceOrRecreate"),
        ],
    )
    def test_vpa_patch_ladder(self, patch_name: str, expected_mode: str) -> None:
        vpa = _find_vpa(_render_with_vpa_patch(patch_name))
        assert vpa["spec"]["updatePolicy"]["updateMode"] == expected_mode

    def test_vpa_disabled_renders_nothing(self) -> None:
        render = _helm_template({"vpa": {"enabled": False}})
        assert all(doc.get("kind") != "VerticalPodAutoscaler" for doc in render)


# ---------------------------------------------------------------------------
# ECS variant assertions
# ---------------------------------------------------------------------------


class TestECSPatch:
    def test_config_patcher_container_present(self, ecs_render: list[dict[str, Any]]) -> None:
        """Regression guard: ECS overlay must re-include prometheus-config-patcher.

        Because prometheusSpec.containers is a full-list Helm override, the ECS
        patch must explicitly redeclare every container it wants to keep.  This
        test is the direct safeguard against the SEV2 regression.
        """
        prom = _find_prometheus_cr(ecs_render)
        names = [c["name"] for c in prom["spec"].get("containers", [])]
        assert "prometheus-config-patcher" in names, (
            f"prometheus-config-patcher missing from ECS prometheusSpec.containers; found: {names}"
        )

    def test_ecs_target_sync_container_present(self, ecs_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(ecs_render)
        names = [c["name"] for c in prom["spec"].get("containers", [])]
        assert "ecs-target-sync" in names, (
            f"ecs-target-sync missing from ECS prometheusSpec.containers; found: {names}"
        )

    def test_auth_proxy_container_absent(self, ecs_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(ecs_render)
        names = [c["name"] for c in prom["spec"].get("containers", [])]
        assert "auth-proxy" not in names, (
            f"auth-proxy nginx sidecar must not be in ECS prometheusSpec.containers; found: {names}"
        )

    def test_required_volumes_present(self, ecs_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(ecs_render)
        vol_names = [v["name"] for v in prom["spec"].get("volumes", [])]
        for required in (
            "ecs-targets",
            "istio-certs",
            # Backs the prometheus-config-patcher script mount; missing here
            # means the container's command points at a script that was
            # never mounted -- same SEV2 shape as the containers-list guard
            # above, just one layer down.
            "config-patcher-script",
        ):
            assert required in vol_names, (
                f"{required} missing from ECS prometheusSpec.volumes; found: {vol_names}"
            )

    def test_retention_time_env_present(self, ecs_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(ecs_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found in ECS render"
        env_names = [e["name"] for e in patcher.get("env", [])]
        assert "RETENTION_TIME" in env_names, (
            f"RETENTION_TIME env var missing from ECS patcher; got: {env_names}"
        )

    def test_mutation_includes_retention_percentage(self, ecs_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(ecs_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found in ECS render"
        mutation = _extract_mutation(ecs_render, patcher)
        assert "retention.percentage = 80" in mutation, (
            f"retention.percentage = 80 not in ECS MUTATION; got: {mutation!r}"
        )

    def test_mutation_excludes_stale_series_threshold(
        self, ecs_render: list[dict[str, Any]]
    ) -> None:
        prom = _find_prometheus_cr(ecs_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found in ECS render"
        mutation = _extract_mutation(ecs_render, patcher)
        assert mutation, "MUTATION could not be located in any rendered ConfigMap"
        assert "stale_series_compaction_threshold" not in mutation, (
            f"stale_series_compaction_threshold must not be in ECS MUTATION; got: {mutation!r}"
        )

    def test_no_hardcoded_retention_size(self, ecs_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(ecs_render)
        assert "retentionSize" not in prom["spec"], (
            "ECS prometheusSpec.retentionSize must not be set"
        )
