"""
Golden-render tests for alchemy-observability-core VPA wiring.

Prerequisites:
  - `helm` must be on PATH.
  - Chart dependencies must be present (charts/ directory populated):
      helm dependency build alchemy-observability-core
    CI caches charts/ between runs; run once locally after a fresh clone.

Guards for templates/vpa.yaml and the vpa-* patch ladder: the VPA must
target the Prometheus CR (never the operator-owned StatefulSet), every
sidecar must be pinned out of the default container policy, and the
patch ladder must move updateMode through Initial -> Recreate ->
InPlaceOrRecreate without rendering drift.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]
CHART_DIR = REPO_ROOT / "alchemy-observability-core"
PATCHES_DIR = CHART_DIR / "patches"

# Top-level alias key used by the per-cluster patch ConfigMaps: the rendered
# instance chart wires alchemy-observability-core in as a dependency under
# this name. The VPA patches carry no ${VAR} placeholders.
_CORE_ALIAS = "alchemy-observability-core"

_PROM_CR_NAME = "prometheus-core"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_core_alias(raw: dict[str, Any]) -> dict[str, Any]:
    """Promote the dependency-aliased block to the top level.

    When running helm template against the source chart (which is not nested
    under the instance wrapper's alias), flatten that extra nesting so the
    values reach ``vpa.*`` / ``kube-prometheus-stack.*`` directly.
    """
    core_block = raw.pop(_CORE_ALIAS, {})
    raw.update(core_block)
    return raw


def _load_patch_values(patch_name: str) -> dict[str, Any]:
    """Extract dependency-aliased values from a patch ConfigMap wrapper."""
    cm = yaml.safe_load((PATCHES_DIR / patch_name).read_text())
    embedded_yaml: str = cm["data"]["values.yaml"]
    return _flatten_core_alias(yaml.safe_load(embedded_yaml))


def _load_vpa_patch_values(patch_name: str) -> dict[str, Any]:
    """Backward-compatible wrapper for the VPA patch assertions."""
    return _load_patch_values(patch_name)


def _helm_template(*extra_values: dict[str, Any], release_name: str = "r") -> list[dict[str, Any]]:
    """Run ``helm template`` against the core chart with optional extra values.

    The chart's own values.yaml is always applied first; each entry in
    *extra_values* is written to a temp file and appended as a ``-f`` flag
    (last file wins for scalar / list collisions, matching Helm's semantics).
    """
    if not (CHART_DIR / "charts").exists():
        pytest.skip(
            "alchemy-observability-core/charts/ not present — "
            "run: helm dependency build alchemy-observability-core"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        value_files: list[str] = ["-f", str(CHART_DIR / "values.yaml")]
        for i, vals in enumerate(extra_values):
            vf = tmp / f"extra-{i}.yaml"
            vf.write_text(yaml.dump(vals, default_flow_style=False))
            value_files += ["-f", str(vf)]

        result = subprocess.run(
            ["helm", "template", release_name, str(CHART_DIR), *value_files],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(f"helm template failed:\nSTDERR:\n{result.stderr}")

        return [d for d in yaml.safe_load_all(result.stdout) if d is not None]


def _find_vpa(docs: list[dict[str, Any]]) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == "VerticalPodAutoscaler":
            return doc
    kinds = [f"{d.get('kind')}/{d.get('metadata', {}).get('name')}" for d in docs]
    pytest.fail(f"No VerticalPodAutoscaler found in rendered output. Kinds present: {kinds}")


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


def _render_with_vpa_patch(patch_name: str) -> list[dict[str, Any]]:
    return _helm_template(_load_patch_values(patch_name))


# ---------------------------------------------------------------------------
# Shared fixtures (module-scoped to run helm template once per variant)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_render() -> list[dict[str, Any]]:
    """Manifests rendered from the core chart's checked-in defaults."""
    return _helm_template()


@pytest.fixture(scope="module")
def istio_monitoring_render() -> list[dict[str, Any]]:
    """Manifests rendered with the Istio monitoring overlay."""
    return _helm_template(_load_patch_values("istio-monitoring.yaml"))


# ---------------------------------------------------------------------------
# VPA variant assertions
# ---------------------------------------------------------------------------


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
            "name": _PROM_CR_NAME,
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
        assert policies["prometheus"]["controlledResources"] == ["cpu", "memory"]
        assert policies["prometheus"]["controlledValues"] == "RequestsAndLimits"
        assert policies["prometheus"]["minAllowed"] == {"cpu": "100m", "memory": "2Gi"}
        assert policies["prometheus"]["maxAllowed"] == {"cpu": "12000m", "memory": "72Gi"}
        for sidecar in ("config-reloader", "istio-proxy", "prometheus-config-patcher", "*"):
            assert policies[sidecar]["mode"] == "Off", f"{sidecar} must be pinned to mode Off"

    def test_min_replicas_clamped_for_single_replica(self) -> None:
        """Fleet base patch sets replicas: 1; minReplicas must stay at 2."""
        patch_vals = _load_vpa_patch_values("vpa-base.yaml")
        patch_vals.setdefault("kube-prometheus-stack", {}).setdefault("prometheus", {}).setdefault(
            "prometheusSpec", {}
        )["replicas"] = 1
        vpa = _find_vpa(_helm_template(patch_vals))
        assert vpa["spec"]["updatePolicy"]["minReplicas"] == 2

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

    def test_vpa_operator_rendered_by_default(self, base_render: list[dict[str, Any]]) -> None:
        assert any(
            doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "vpa-recommender"
            for doc in base_render
        )
        assert any(
            doc.get("kind") == "Deployment"
            and doc["metadata"]["name"] == "vpa-admission-controller"
            for doc in base_render
        )
        assert not any(
            doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "vpa-updater"
            for doc in base_render
        )

    def test_vpa_certgen_job_names_within_dns_label_limit(self) -> None:
        """Admission certgen hook Job pod template names must fit in 63 chars."""
        long_release = "core-archv2-ops-ovh-stage-use1"
        docs = _helm_template(release_name=long_release)
        certgen_jobs = [
            doc
            for doc in docs
            if doc.get("kind") == "Job" and doc["metadata"]["name"] == "vpa-admission-certgen"
        ]
        assert certgen_jobs, "expected vpa-admission-certgen hook Job"
        job = certgen_jobs[0]
        pod_meta = job["spec"]["template"]["metadata"]
        assert len(job["metadata"]["name"]) <= 63
        assert len(pod_meta["name"]) <= 63
        for key, value in pod_meta.get("labels", {}).items():
            assert len(str(value)) <= 63, f"{key}={value}"

    def test_vpa_certgen_jobs_exclude_istio_sidecar(
        self, base_render: list[dict[str, Any]]
    ) -> None:
        """certGen hook Jobs must opt out of Istio injection in meshed prometheus namespaces."""
        certgen_jobs = [
            doc
            for doc in base_render
            if doc.get("kind") == "Job" and "certgen" in doc["metadata"]["name"]
        ]
        assert len(certgen_jobs) == 2, "expected create + patch certgen hook Jobs"
        for job in certgen_jobs:
            labels = job["spec"]["template"]["metadata"].get("labels", {})
            assert labels.get("sidecar.istio.io/inject") == "false", job["metadata"]["name"]

    def test_vpa_admission_webhook_rendered(self, base_render: list[dict[str, Any]]) -> None:
        webhook = next(
            doc
            for doc in base_render
            if doc.get("kind") == "MutatingWebhookConfiguration"
            and doc["metadata"]["name"] == "vpa-webhook-config"
        )
        assert webhook["webhooks"][0]["name"] == "vpa.k8s.io"

    def test_vpa_install_operator_can_be_disabled(self) -> None:
        render = _helm_template({"vpa": {"installOperator": False}})
        assert not any(
            doc.get("kind") == "Deployment" and doc["metadata"]["name"].startswith("vpa-")
            for doc in render
        )

    def test_vpa_base_installs_operator(self) -> None:
        docs = _render_with_vpa_patch("vpa-base.yaml")
        assert _find_vpa(docs) is not None
        assert not any(
            doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "vpa-updater"
            for doc in docs
        )
        # CRDs live in the vertical-pod-autoscaler subchart crds/ directory;
        # Helm/Argo installs them at sync time (not in plain helm template output).
        with tempfile.TemporaryDirectory() as tmpdir:
            vf = Path(tmpdir) / "vpa-base.yaml"
            vf.write_text(
                yaml.dump(_load_vpa_patch_values("vpa-base.yaml"), default_flow_style=False)
            )
            result = subprocess.run(
                [
                    "helm",
                    "template",
                    "r",
                    str(CHART_DIR),
                    "-f",
                    str(CHART_DIR / "values.yaml"),
                    "-f",
                    str(vf),
                    "--include-crds",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        assert "verticalpodautoscalers.autoscaling.k8s.io" in result.stdout

    @pytest.mark.parametrize("patch_name", ["vpa-active.yaml", "vpa-active-inplace.yaml"])
    def test_active_patches_enable_updater(self, patch_name: str) -> None:
        docs = _render_with_vpa_patch(patch_name)
        assert any(
            doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "vpa-updater"
            for doc in docs
        )


class TestVpaKsmCollector:
    """Guards for the kube-state-metrics CustomResourceState VPA collector.

    KSM removed the built-in verticalpodautoscalers collector in v2.9.0
    (kubernetes/kube-state-metrics#2017); the CRS config + RBAC ship in the
    chart values.yaml so kube_verticalpodautoscaler_* metrics exist before the
    vpa-* patch ladder enables the VPA object.
    """

    @staticmethod
    def _find_ksm_deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
        for doc in docs:
            if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "kube-state-metrics":
                return doc
        pytest.fail("No kube-state-metrics Deployment in rendered output")

    @staticmethod
    def _crs_configmaps(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            doc
            for doc in docs
            if doc.get("kind") == "ConfigMap"
            and "CustomResourceStateMetrics" in (doc.get("data") or {}).get("config.yaml", "")
        ]

    def test_crs_enabled_by_default(self, base_render: list[dict[str, Any]]) -> None:
        cms = self._crs_configmaps(base_render)
        assert len(cms) == 1, "chart defaults must render exactly one CRS ConfigMap"
        config = cms[0]["data"]["config.yaml"]
        # Metric names/labels must reproduce the legacy kube_verticalpodautoscaler_*
        # series the KCV Autoscaling dashboard queries.
        assert "metricNamePrefix: kube" in config
        assert (
            "verticalpodautoscaler_status_recommendation_containerrecommendations_target" in config
        )
        assert "verticalpodautoscaler_spec_updatepolicy_updatemode" in config
        assert "verticalpodautoscaler_spec_resourcepolicy_container_policies_minallowed" in config
        assert "verticalpodautoscaler_spec_resourcepolicy_container_policies_maxallowed" in config
        assert "labelName: update_mode" in config
        assert "InPlaceOrRecreate" in config

    def test_base_render_ksm_deployment_mounts_crs(self, base_render: list[dict[str, Any]]) -> None:
        ksm = self._find_ksm_deployment(base_render)
        args = ksm["spec"]["template"]["spec"]["containers"][0]["args"]
        assert any(a.startswith("--custom-resource-state-config-file=") for a in args)

    def test_base_render_adds_vpa_rbac(self, base_render: list[dict[str, Any]]) -> None:
        for doc in base_render:
            if (
                doc.get("kind") != "ClusterRole"
                or "kube-state-metrics" not in doc["metadata"]["name"]
            ):
                continue
            for rule in doc.get("rules", []):
                if rule.get("apiGroups") == ["autoscaling.k8s.io"] and rule.get("resources") == [
                    "verticalpodautoscalers"
                ]:
                    assert set(rule.get("verbs", [])) >= {"list", "watch"}
                    return
        pytest.fail("No autoscaling.k8s.io/verticalpodautoscalers rule in KSM ClusterRole")


class TestAutoGoMemlimit:
    """The VPA contract depends on Prometheus following the cgroup envelope."""

    def test_gomemlimit_ratio_pinned(self, base_render: list[dict[str, Any]]) -> None:
        """--auto-gomemlimit.ratio must stay visible in additionalArgs.

        auto-gomemlimit/auto-gomaxprocs are default-on since Prometheus v3.0;
        the explicit ratio pin documents that GOMEMLIMIT tracks the container
        memory limit VPA resizes (RequestsAndLimits).
        """
        prom = _find_prometheus_cr(base_render)
        args = {a["name"]: a.get("value") for a in prom["spec"].get("additionalArgs", [])}
        assert args.get("auto-gomemlimit.ratio") == "0.9"


class TestConfigPatcher:
    """Guards for prometheus-config-patcher sidecar wiring."""

    def test_config_patcher_container_present(self, base_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(base_render)
        names = [c["name"] for c in prom["spec"].get("containers", [])]
        assert "prometheus-config-patcher" in names, (
            f"prometheus-config-patcher missing from prometheusSpec.containers; found: {names}"
        )

    def test_mutation_includes_compaction_threshold(self, base_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(base_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found"
        mutation = _extract_mutation(patcher)
        assert "stale_series_compaction_threshold" in mutation, (
            f"stale_series_compaction_threshold not in MUTATION; got: {mutation!r}"
        )

    def test_config_patcher_script_configmap_present(
        self, base_render: list[dict[str, Any]]
    ) -> None:
        cm = next(
            (
                doc
                for doc in base_render
                if doc.get("kind") == "ConfigMap"
                and doc["metadata"]["name"] == "prometheus-core-config-patcher-script"
            ),
            None,
        )
        assert cm is not None, "prometheus-core-config-patcher-script ConfigMap not found"
        assert "config-patcher.sh" in cm.get("data", {})

    def test_config_patcher_script_mount_present(self, base_render: list[dict[str, Any]]) -> None:
        prom = _find_prometheus_cr(base_render)
        patcher = _find_container(prom["spec"].get("containers", []), "prometheus-config-patcher")
        assert patcher is not None, "prometheus-config-patcher container not found"
        mount_names = [m["name"] for m in patcher.get("volumeMounts", [])]
        assert "config-patcher-script" in mount_names, (
            f"config-patcher-script volumeMount missing; got: {mount_names}"
        )
        volume_names = [v["name"] for v in prom["spec"].get("volumes", [])]
        assert "config-patcher-script" in volume_names, (
            f"config-patcher-script volume missing; got: {volume_names}"
        )

    def test_istio_overlay_keeps_config_patcher_volume(
        self, istio_monitoring_render: list[dict[str, Any]]
    ) -> None:
        prom = _find_prometheus_cr(istio_monitoring_render)
        volume_names = [v["name"] for v in prom["spec"].get("volumes", [])]
        assert "config-patcher-script" in volume_names, (
            "istio-monitoring.yaml replaces prometheusSpec.volumes, so it must retain "
            f"config-patcher-script; got: {volume_names}"
        )
