"""Render assertions for the observability capacity alerts' Grafana wiring.

Prerequisites:
  - `helm` must be on PATH.
  - Chart dependencies must be present (charts/ directory populated):
      helm dependency build alchemy-observability-core
      helm dependency build alchemy-observability-agent
    CI's render gate dependency-builds every chart before this suite runs.

The four `observability-capacity-alerts` rules are the ones that page oncall for
a Prometheus running out of CPU or memory. Two separate consumers read the
rendered rule, and each one needs a different field:

  1. The `annotation_bridge` webhook (cloud-infra-tools) writes a Grafana
     annotation for a firing alert. It reads **labels** only, and skips any
     alert whose `annotate` label is not exactly "true". A `dashboardUID` in
     the `annotations:` block is invisible to it.
  2. The Alertmanager Slack template (`alertmanager/templates/
     alertmanager-slack-templates.yaml`) renders the "View in Grafana" button
     from the `dashboard_url` **annotation**, falling back to a bare
     `/explore` pane when it is absent.

So these tests assert both halves: the labels the bridge needs, and the exact
`dashboard_url` annotation the Slack template needs. The URL is asserted
literally rather than by substring because every query parameter is
load-bearing:

  - `var-ptype` differs per chart and must be percent-encoded. Every panel
    selects on exact `pod="$pod"`, so `var-pod` without a resolvable
    `var-ptype` leaves the pod picker unable to resolve.
  - `$externalLabels.cluster`, not `$labels.cluster`. On a core Prometheus,
    `container_cpu_usage_seconds_total` comes from local cadvisor and carries
    no `cluster` label, so `$labels.cluster` renders empty.
  - `var-resolution=1m`; the dashboard's 5m default smooths short spikes out
    of view.
  - No `var-datasource`: its `current` is empty with regex `/thanos.*/`, so
    Grafana resolves it on load. A hardcoded datasource UID would be worse.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]

_ALERTS_TEMPLATE = "templates/default-alerts.yaml"
_CAPACITY_GROUP = "observability-capacity-alerts"

_DASHBOARD_UID = "observability-prometheus-deep-dive"

# Every rule in the capacity group. Enumerated explicitly so a rule silently
# dropped from the group fails the suite instead of shrinking its coverage.
CAPACITY_ALERTS: tuple[str, ...] = (
    "PrometheusHighCPUUsage",
    "PrometheusHighCPUAndThrottled",
    "PrometheusHighMemoryUsage",
    "PrometheusVeryHighMemoryUsage",
)

# Percent-encoded `$ptype` option values from the dashboard generator
# (cloud-infra-tools grafana_dashboards/teams/observability/dashboards/
# prometheus_deep_dive.py, `_PTYPE_OPTIONS`). Regex metacharacters are encoded
# because `|` is not legal in a query string and a literal `+` decodes to a
# space.
#   Core  : prometheus-.*core.*
#   Agent : prometheus-(istio|nodegw)-.+
_CORE_PTYPE = "prometheus-.%2Acore.%2A"
_AGENT_PTYPE = "prometheus-%28istio%7Cnodegw%29-.%2B"


def _expected_dashboard_url(ptype: str) -> str:
    return (
        "https://grafana.obs-public.i.alchemy.com"
        f"/d/{_DASHBOARD_UID}/prometheus-deep-dive"
        f"?var-ptype={ptype}"
        "&var-cluster={{ $externalLabels.cluster | urlquery }}"
        "&var-pod={{ $labels.pod | urlquery }}"
        "&var-resolution=1m&from=now-3h&to=now"
    )


CHART_CASES = (
    ("alchemy-observability-core", _CORE_PTYPE),
    ("alchemy-observability-agent", _AGENT_PTYPE),
)


def _render_capacity_rules(chart: str) -> dict[str, dict[str, Any]]:
    """Return the capacity-group rules of a chart's default-alerts.yaml by name."""
    chart_dir = REPO_ROOT / chart
    if not (chart_dir / "charts").exists():
        pytest.skip(f"{chart}/charts/ not present — run: helm dependency build {chart}")

    result = subprocess.run(
        ["helm", "template", "r", str(chart_dir), "--show-only", _ALERTS_TEMPLATE],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template {chart} failed:\nSTDERR:\n{result.stderr}")

    docs = [d for d in yaml.safe_load_all(result.stdout) if d is not None]
    rule_docs = [d for d in docs if d.get("kind") == "PrometheusRule"]
    if not rule_docs:
        kinds = [d.get("kind") for d in docs]
        pytest.fail(f"No kind:PrometheusRule rendered from {chart}. Kinds: {kinds}")

    groups = [
        g for doc in rule_docs for g in doc["spec"]["groups"] if g.get("name") == _CAPACITY_GROUP
    ]
    if not groups:
        names = [g.get("name") for doc in rule_docs for g in doc["spec"]["groups"]]
        pytest.fail(f"No {_CAPACITY_GROUP} group in {chart}. Groups: {names}")

    return {r["alert"]: r for g in groups for r in g["rules"]}


@pytest.fixture(scope="module", params=CHART_CASES, ids=[c for c, _ in CHART_CASES])
def capacity_case(request: pytest.FixtureRequest) -> tuple[str, str, dict[str, dict[str, Any]]]:
    chart, ptype = request.param
    return chart, ptype, _render_capacity_rules(chart)


def _rule(
    capacity_case: tuple[str, str, dict[str, dict[str, Any]]], alertname: str
) -> tuple[str, str, dict[str, Any]]:
    chart, ptype, rules = capacity_case
    assert alertname in rules, (
        f"{alertname} missing from {chart} {_CAPACITY_GROUP}; found: {sorted(rules)}"
    )
    return chart, ptype, rules[alertname]


@pytest.mark.parametrize("alertname", CAPACITY_ALERTS)
def test_dashboard_url_annotation_is_exact(
    capacity_case: tuple[str, str, dict[str, dict[str, Any]]], alertname: str
) -> None:
    """The Slack "View in Grafana" button reads Annotations.dashboard_url."""
    chart, ptype, rule = _rule(capacity_case, alertname)
    annotations = rule.get("annotations", {})
    assert "dashboard_url" in annotations, (
        f"{chart}/{alertname} has no dashboard_url annotation — the Slack "
        f'"View in Grafana" button falls through to a bare /explore pane. '
        f"Annotations present: {sorted(annotations)}"
    )
    assert annotations["dashboard_url"] == _expected_dashboard_url(ptype)


@pytest.mark.parametrize("alertname", CAPACITY_ALERTS)
def test_annotate_label_present(
    capacity_case: tuple[str, str, dict[str, dict[str, Any]]], alertname: str
) -> None:
    """annotation_bridge skips any alert whose `annotate` label is not "true"."""
    chart, _, rule = _rule(capacity_case, alertname)
    labels = rule.get("labels", {})
    assert labels.get("annotate") == "true", (
        f'{chart}/{alertname} needs label annotate: "true" for annotation_bridge '
        f"to write a Grafana annotation; labels are: {labels}"
    )


@pytest.mark.parametrize("alertname", CAPACITY_ALERTS)
def test_dashboard_uid_is_a_label(
    capacity_case: tuple[str, str, dict[str, dict[str, Any]]], alertname: str
) -> None:
    """annotation_bridge reads dashboardUID from labels, never from annotations."""
    chart, _, rule = _rule(capacity_case, alertname)
    labels = rule.get("labels", {})
    assert labels.get("dashboardUID") == _DASHBOARD_UID, (
        f"{chart}/{alertname} needs label dashboardUID: {_DASHBOARD_UID}; labels are: {labels}"
    )


@pytest.mark.parametrize("alertname", CAPACITY_ALERTS)
def test_dashboard_uid_not_left_in_annotations(
    capacity_case: tuple[str, str, dict[str, dict[str, Any]]], alertname: str
) -> None:
    """A dashboardUID under annotations: is inert — no consumer reads it there."""
    chart, _, rule = _rule(capacity_case, alertname)
    annotations = rule.get("annotations", {})
    assert "dashboardUID" not in annotations, (
        f"{chart}/{alertname} still carries dashboardUID as an annotation, where "
        f"nothing reads it. It belongs in labels."
    )
