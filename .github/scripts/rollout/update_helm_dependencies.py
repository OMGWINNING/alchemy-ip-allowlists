#! /usr/bin/env python3
"""Update Helm Chart Dependencies to Latest Versions

Usage:
    .github/scripts/rollout/update_helm_dependencies.py --check
    .github/scripts/rollout/update_helm_dependencies.py --chart core [--download] [--dry-run]
    .github/scripts/rollout/update_helm_dependencies.py --update-all [--download] [--dry-run]
    .github/scripts/rollout/update_helm_dependencies.py --customer-dashboards [--dry-run]
    .github/scripts/rollout/update_helm_dependencies.py --infra
    .github/scripts/rollout/update_helm_dependencies.py --infra-update <chart-dir> [--dry-run]
"""

import argparse
import contextlib
import json
import pathlib
import subprocess
import sys
import yaml
from loguru import logger

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lib.common import BaseChartManager, add_common_arguments

CHART_DEPENDENCIES = {
    "core": ["kube-prometheus-stack", "metrics-server", "prometheus-adapter", "alloy"],
    "agent": ["kube-prometheus-stack"],
    "shard": ["kube-prometheus-stack"],
    "grafana": ["grafana"],
}

REPOS = {
    "kube-prometheus-stack": "https://prometheus-community.github.io/helm-charts",
    "metrics-server": "https://kubernetes-sigs.github.io/metrics-server",
    "prometheus-adapter": "https://prometheus-community.github.io/helm-charts",
    "grafana": "https://grafana-community.github.io/helm-charts",
    "alloy": "https://grafana.github.io/helm-charts",
}

# Infra charts covered by --infra / --infra-update. Each must have a tracked
# Chart.yaml whose first `dependencies:` entry holds the upstream dep name + repo
# URL — pulled at runtime so a URL move (loki/tracing → grafana-community) needs
# no script edit. heimdall/ stays out (no `dependencies:` block).
INFRA_CHART_DIRS = [
    "istio-base",
    "istiod",
    "istio-gateway",
    "loki",
    "tracing",
    "kiali",
    "cert-manager",
    "external-secrets",
    "cluster-autoscaler",
    "aws-load-balancer-controller",
    "wiz",
]

# Charts where a multi-minor jump (delta > 1) is also MAJOR_JUMP. Limited to
# loki + tracing — both have a history of breaking minor changes (loki 9→12
# chart-fork at v6.55.0, tempo vParquet/metricsGenerator reshapes).
INFRA_HUMAN_REVIEW_CHARTS = {"loki", "tracing"}

# Maps customer-dashboard config.yaml keys to local chart names (in common.CHARTS)
CUSTOMER_DASHBOARD_DEPS = {
    "grafanaChartVersion": "grafana",
    "shardChartVersion": "shard",
}


def _strip_v(v: str) -> str:
    return v.removeprefix("v") if v else v


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'v1.2.3-rc1' → (1, 2, 3). Returns () on non-semver.

    Pre-release suffix is dropped — fine while `helm search repo` excludes
    pre-releases by default; revisit if `--devel` ever gets wired in.
    """
    parts = _strip_v(v or "").split("-")[0].split(".")[:3]
    return tuple(int(x) for x in parts if x.isdigit())


def _classify_drift(current: str, latest: str, chart_dir: str) -> str | None:
    """Return 'AHEAD', 'MAJOR_JUMP', or None when versions compare normally."""
    cur, lat = _parse_version(current), _parse_version(latest)
    if not cur or not lat:
        return None
    if cur > lat:
        return "AHEAD"
    if lat[0] > cur[0]:
        return "MAJOR_JUMP"
    if (
        chart_dir in INFRA_HUMAN_REVIEW_CHARTS
        and len(lat) > 1
        and len(cur) > 1
        and lat[1] - cur[1] > 1
    ):
        return "MAJOR_JUMP"
    return None


class HelmUpdater(BaseChartManager):
    def run_cmd(self, cmd, cwd=None, capture=False):
        """Override to allow helm commands even in dry-run"""
        if self.dry_run and cmd[0] != "helm":
            logger.info(f"[DRY RUN] {' '.join(cmd)}")
            return None
        return super().run_cmd(cmd, cwd, capture)

    def is_pinned_dependency(self, chart_name, dep_name):
        chart_file = self.get_chart_file(chart_name)
        in_dep = False
        with open(chart_file) as f:
            for raw_line in f:
                line = raw_line.strip()
                if line.startswith("- name:"):
                    in_dep = line.split(":", 1)[1].strip() == dep_name
                    continue
                if in_dep and line.startswith("version:"):
                    return "pin" in raw_line.lower()
        return False

    def check_latest(self, dep_name):
        repo_url = REPOS.get(dep_name)
        if not repo_url:
            return None
        return self._search_helm_repo(dep_name, repo_url)

    def _search_helm_repo(self, dep_name: str, repo_url: str) -> str | None:
        """Query upstream for latest version of `dep_name`; clean up temp alias.

        `--force-update` survives a stale alias from an interrupted prior run;
        the inner try in `finally` swallows cleanup failures so they don't
        mask the real exception.
        """
        repo_alias = f"temp-{dep_name}"
        self.run_cmd(["helm", "repo", "add", "--force-update", repo_alias, repo_url])
        try:
            self.run_cmd(["helm", "repo", "update", repo_alias])
            output = self.run_cmd(
                ["helm", "search", "repo", f"{repo_alias}/{dep_name}", "--output", "json"],
                capture=True,
            )
        finally:
            # don't mask the real exception if cleanup itself fails
            with contextlib.suppress(subprocess.CalledProcessError):
                self.run_cmd(["helm", "repo", "remove", repo_alias])
        if not output:
            return None
        # `helm search repo` does substring matching, so filter to the exact
        # chart name (e.g. `loki` would otherwise match `loki-canary` too).
        full_name = f"{repo_alias}/{dep_name}"
        results = [r for r in json.loads(output) if r["name"] == full_name]
        return results[0]["version"] if results else None

    def get_current_versions(self, chart_name):
        data = self.read_chart(chart_name)
        return {dep["name"]: dep["version"] for dep in data.get("dependencies", [])}

    def update_dependency(self, chart_name, dep_name, new_version):
        if self.is_pinned_dependency(chart_name, dep_name):
            logger.info(f"{dep_name}: pinned, skipping update")
            return False
        data = self.read_chart(chart_name)

        updated = False
        for dep in data.get("dependencies", []):
            if dep["name"] == dep_name and dep["version"] != new_version:
                logger.success(f"{dep_name}: {dep['version']} → {new_version}")
                dep["version"] = new_version
                updated = True

        if updated:
            self.write_chart(chart_name, data)
        return updated

    def download_dependencies(self, chart_name):
        if self.dry_run:
            logger.info(f"[DRY RUN] Would run helm dependency update for {chart_name}")
            return
        chart_dir = self.get_chart_path(chart_name)
        self.run_cmd(["helm", "dependency", "update"], cwd=chart_dir)

    def _read_yaml(self, chart_dir: str, filename: str) -> dict | None:
        """Read a YAML file from an infra chart dir. None if missing/unreadable."""
        path = self.repo_root / chart_dir / filename
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            logger.error(f"{chart_dir}/{filename}: parse error: {e}")
            return None

    def _infra_version(self, chart_dir: str, dep_name: str, source: str) -> str | None:
        """Version of `dep_name` in `source` (Chart.yaml or Chart.lock).

        Wrapper charts may declare the same dep name multiple times under aliases
        (istio-gateway has six). Returns the first match; warns on drift.
        """
        data = self._read_yaml(chart_dir, source)
        if not data:
            return None
        versions = [
            d.get("version") for d in data.get("dependencies", []) if d.get("name") == dep_name
        ]
        if not versions:
            return None
        if len(set(versions)) > 1:
            logger.warning(
                f"{chart_dir}/{source}: drifted {dep_name!r} versions {sorted(set(versions))}"
            )
        return versions[0]

    def _infra_primary_dep(self, chart_dir: str) -> tuple[str, str] | None:
        """Return (dep_name, repo_url) from the first dependency entry in Chart.yaml."""
        data = self._read_yaml(chart_dir, "Chart.yaml")
        deps = (data or {}).get("dependencies", [])
        if not deps:
            return None
        name, repo = deps[0].get("name"), deps[0].get("repository")
        return (name, repo) if name and repo else None

    def check_infra(self) -> bool:
        """Check infra charts vs upstream; print markdown table.

        Returns True (exit 1 desired) if any status is BEHIND / STALE_LOCK / ERROR.
        AHEAD and MAJOR_JUMP are informational — log warning, don't flip exit code.
        """
        rows = []
        has_issues = False
        for chart_dir in INFRA_CHART_DIRS:
            primary = self._infra_primary_dep(chart_dir)
            if not primary:
                rows.append((chart_dir, "unknown", "none", "unknown", "unknown", "ERROR"))
                has_issues = True
                continue
            dep_name, repo_url = primary
            cv = self._infra_version(chart_dir, dep_name, "Chart.yaml") or "unknown"
            lv = self._infra_version(chart_dir, dep_name, "Chart.lock")  # may be None
            try:
                latest = self._search_helm_repo(dep_name, repo_url) or "unknown"
            except subprocess.CalledProcessError as e:
                logger.error(f"{chart_dir}: upstream search failed: {e}")
                latest = "unknown"

            if cv == "unknown" or latest == "unknown":
                status = "ERROR"
            elif lv is not None and _strip_v(lv) != _strip_v(cv):
                status = "STALE_LOCK"
            elif _strip_v(cv) == _strip_v(latest):
                status = "OK"
            else:
                status = _classify_drift(cv, latest, chart_dir) or "BEHIND"

            rows.append((chart_dir, dep_name, lv or "none", cv, latest, status))
            if status in ("BEHIND", "STALE_LOCK", "ERROR"):
                has_issues = True

        print(
            "| chart_dir | dep_name | lock_version | chart_yaml_version | latest_version | status |"
        )
        print("|---|---|---|---|---|---|")
        for row in rows:
            print(f"| {' | '.join(str(c) for c in row)} |")
        for chart_dir, _, _, cv, latest, status in rows:
            if status in ("MAJOR_JUMP", "AHEAD"):
                logger.warning(f"{chart_dir}: {status} ({cv} vs upstream {latest})")
            elif status == "ERROR":
                logger.error(f"{chart_dir}: ERROR (chart_yaml={cv}, latest={latest})")
        return has_issues

    def update_infra_chart(self, chart_dir: str) -> bool:
        """Bump a chart's Chart.yaml to upstream-latest + run helm dep update.

        True on success or no-op (already at latest); False on real error
        (caller can `sys.exit(1)` on False).
        """
        if chart_dir not in INFRA_CHART_DIRS:
            logger.error(f"Unknown infra chart: {chart_dir!r}. Valid: {INFRA_CHART_DIRS}")
            return False
        primary = self._infra_primary_dep(chart_dir)
        if not primary:
            logger.error(f"{chart_dir}: no usable primary dep in Chart.yaml")
            return False
        dep_name, repo_url = primary

        current = self._infra_version(chart_dir, dep_name, "Chart.yaml")
        logger.info(f"{chart_dir}: current {dep_name} = {current}")
        try:
            latest = self._search_helm_repo(dep_name, repo_url)
        except subprocess.CalledProcessError as e:
            logger.error(f"{chart_dir}: upstream search failed: {e}")
            return False
        if not latest:
            logger.error(f"{chart_dir}: could not determine latest for {dep_name}")
            return False
        if _strip_v(current or "") == _strip_v(latest):
            logger.info(f"{chart_dir}: {dep_name} already at latest ({latest})")
            # Only refresh Chart.lock if it's actually stale — operator may have
            # run --infra-update precisely to resolve a STALE_LOCK from --infra.
            lock_ver = self._infra_version(chart_dir, dep_name, "Chart.lock")
            if lock_ver is not None and _strip_v(lock_ver) != _strip_v(latest):
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would refresh Chart.lock for {chart_dir}")
                else:
                    self.run_cmd(["helm", "dependency", "update"], cwd=self.repo_root / chart_dir)
            return True
        if _classify_drift(current or "", latest, chart_dir) == "MAJOR_JUMP":
            logger.warning(f"{chart_dir}: major jump {current} → {latest} — verify changelog")

        data = self._read_yaml(chart_dir, "Chart.yaml") or {}
        updated = False
        for dep in data.get("dependencies", []):
            if dep["name"] == dep_name:
                if not updated:  # log once across aliases
                    logger.success(f"{chart_dir}: {dep_name} {dep['version']} → {latest}")
                dep["version"] = latest
                updated = True
        if not updated:
            logger.error(f"{chart_dir}: no dep named {dep_name!r} in Chart.yaml")
            return False

        chart_path = self.repo_root / chart_dir / "Chart.yaml"
        if self.dry_run:
            logger.info(f"[DRY RUN] Would write {chart_path} + helm dep update")
            return True
        with open(chart_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        self.run_cmd(["helm", "dependency", "update"], cwd=self.repo_root / chart_dir)
        return True

    def update_customer_dashboards(self):
        """Update customer dashboard config.yaml files with local chart versions"""
        config_files = list(
            pathlib.Path(self.repo_root).glob("helm/customer-dashboards/**/config.yaml")
        )
        if not config_files:
            logger.info("No customer dashboard config files found")
            return False

        # Read current versions from local Chart.yaml files
        latest = {}
        for config_key, chart_name in CUSTOMER_DASHBOARD_DEPS.items():
            version = self.get_version(chart_name)
            latest[config_key] = version
            logger.info(f"Local {chart_name}: {version}")

        any_updated = False
        for config_file in config_files:
            with open(config_file) as f:
                config = yaml.safe_load(f)

            updated = False
            for key, new_version in latest.items():
                if key in config and config[key] != new_version:
                    logger.success(
                        f"{config_file.relative_to(self.repo_root)}: "
                        f"{key}: {config[key]} → {new_version}"
                    )
                    config[key] = new_version
                    updated = True

            if updated:
                any_updated = True
                if not self.dry_run:
                    with open(config_file, "w") as f:
                        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        return any_updated

    def check_customer_dashboards(self):
        """Check customer dashboard config versions against local charts (read-only)"""
        config_files = list(
            pathlib.Path(self.repo_root).glob("helm/customer-dashboards/**/config.yaml")
        )
        if not config_files:
            return

        print("\nCUSTOMER DASHBOARDS:")
        latest = {}
        for config_key, chart_name in CUSTOMER_DASHBOARD_DEPS.items():
            latest[config_key] = self.get_version(chart_name)

        for config_file in config_files:
            rel_path = config_file.relative_to(self.repo_root)
            print(f"\n  {rel_path}:")
            with open(config_file) as f:
                config = yaml.safe_load(f)

            for config_key, chart_name in CUSTOMER_DASHBOARD_DEPS.items():
                curr_ver = config.get(config_key, "unknown")
                local_ver = latest.get(config_key)
                status = "✓" if curr_ver == local_ver else "⚠"
                logger.info(f"    {status} {chart_name}: {curr_ver} → {local_ver}")


def main():
    parser = argparse.ArgumentParser(
        description="Update Helm dependencies",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_arguments(parser)
    parser.add_argument("--update-all", action="store_true", help="Update all charts")
    parser.add_argument("--check", action="store_true", help="Check for updates without applying")
    parser.add_argument(
        "--download", action="store_true", help="Download dependencies after update"
    )
    parser.add_argument(
        "--customer-dashboards",
        action="store_true",
        help="Update customer dashboard chart versions from local charts",
    )
    parser.add_argument(
        "--infra",
        action="store_true",
        help="Check infra chart versions against upstream (read-only); exits 1 on BEHIND/STALE_LOCK/ERROR",
    )
    parser.add_argument(
        "--infra-update",
        metavar="CHART_DIR",
        help="Update a single infra chart's Chart.yaml to latest and run helm dependency update",
    )
    args = parser.parse_args()

    updater = HelmUpdater(dry_run=args.dry_run)

    if args.infra:
        if updater.check_infra():
            sys.exit(1)
        return

    if args.infra_update:
        if not updater.update_infra_chart(args.infra_update):
            sys.exit(1)  # real error; no-op success returns True
        logger.success("Done!")
        return

    charts = (
        list(CHART_DEPENDENCIES.keys())
        if args.update_all
        else ([args.chart] if args.chart else list(CHART_DEPENDENCIES.keys()))
    )

    # Check mode
    if args.check or not (args.chart or args.update_all or args.customer_dashboards):
        for chart in charts:
            print(f"\n{chart.upper()}:")
            current = updater.get_current_versions(chart)
            for dep in CHART_DEPENDENCIES[chart]:
                curr_ver = current.get(dep, "unknown")
                if updater.is_pinned_dependency(chart, dep):
                    logger.info(f"  ⏭ {dep}: {curr_ver} (pinned)")
                    continue
                latest_ver = updater.check_latest(dep)
                if latest_ver:
                    status = "✓" if curr_ver == latest_ver else "⚠"
                    logger.info(f"  {status} {dep}: {curr_ver} → {latest_ver}")
        updater.check_customer_dashboards()
        return

    # Update base chart dependencies
    if args.chart or args.update_all:
        logger.info(f"Updating: {', '.join(charts)}")

        version_map = {}
        for chart in charts:
            for dep in CHART_DEPENDENCIES[chart]:
                if updater.is_pinned_dependency(chart, dep):
                    logger.info(f"{dep}: pinned, skipping update check")
                    continue
                if dep not in version_map:
                    version_map[dep] = updater.check_latest(dep)

        for chart in charts:
            any_updated = False
            for dep in CHART_DEPENDENCIES[chart]:
                if (
                    dep in version_map
                    and version_map[dep]
                    and updater.update_dependency(chart, dep, version_map[dep])
                ):
                    any_updated = True

            if args.download and any_updated:
                updater.download_dependencies(chart)

    # Update customer dashboard chart versions
    if args.update_all or args.customer_dashboards:
        logger.info("Updating customer dashboard dependencies...")
        updater.update_customer_dashboards()

    logger.success("Done!")


if __name__ == "__main__":
    main()
