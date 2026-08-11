#! /usr/bin/env python3
"""Create Dependency Update PRs

Orchestrates the creation of PRs for helm chart dependency updates in a controlled rollout.

Separates shards from core+agents for independent rollout control.

Standard rollout (11 PRs):
  1. Update dependencies and images for all charts
  2. Rollout shards to stage
  3. Rollout core+agents to stage
  4. Rollout shards to prod: usw2
  5. Rollout core+agents to prod: usw2
  6. Rollout shards to prod: euc1, euc2, apse1
  7. Rollout core+agents to prod: euc1, euc2, apse1
  8. Rollout shards to prod: use1
  9. Rollout core+agents to prod: use1
  10. Update snowflake production config
  11. Update customer dashboard chart versions

Fast rollout (7 PRs, --fast):
  1. Update dependencies and images for all charts
  2. Rollout shards to stage
  3. Rollout core+agents to stage
  4. Rollout shards to prod: all regions at once
  5. Rollout core+agents to prod: all regions at once
  6. Update snowflake production config
  7. Update customer dashboard chart versions

Usage:
    .github/scripts/rollout/create_update_prs.py [--dry-run] [--fast]
    .github/scripts/rollout/create_update_prs.py --stage-only [--stage-charts core,agent,shard]
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import get_repo_root

REPO_ROOT = get_repo_root()
CHARTS = ["core", "agent", "shard", "grafana"]
SHARD_CHARTS = ["shard"]
CORE_AGENT_CHARTS = ["core", "agent"]
ALCHEMY_CHART_DIRS = [f"alchemy-observability-{chart}" for chart in CHARTS]


class PRCreator:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.pr_number = 0
        self.total_prs = 0
        self.branch_names = []
        self.chart_versions = {}  # Store bumped chart versions {chart_name: version}
        self._ghcr_authenticated = False

    def run_cmd(self, cmd, cwd=None, capture=False):
        if self.dry_run and (cmd[0] == "git" or cmd[0] in ["uv", "gh"]):
            logger.info(f"[DRY RUN] {' '.join(cmd)}")
            return None
        result = subprocess.run(
            cmd, cwd=cwd or REPO_ROOT, capture_output=capture, text=True, check=True
        )
        return result.stdout if capture else None

    def get_pinned_deps_from_chart(self, chart_path):
        pinned = set()
        if not chart_path.exists():
            return pinned
        in_dep = False
        current_dep = None
        with open(chart_path) as f:
            for raw_line in f:
                line = raw_line.strip()
                if line.startswith("- name:"):
                    current_dep = line.split(":", 1)[1].strip()
                    in_dep = True
                    continue
                if in_dep and line.startswith("version:"):
                    if "pin" in raw_line.lower() and current_dep:
                        pinned.add(current_dep)
                    in_dep = False
                    current_dep = None
        return pinned

    def update_chart_versions_preserving_comments(self, chart_path, version_updates):
        """Update dependency versions in a Chart.yaml while preserving comments.

        ``version_updates`` maps dependency name (or alias) to the new version string.
        Only the ``version:`` line of matching dependencies is rewritten; every other
        line (including inline comments on non-version lines) is kept verbatim.
        """
        lines = chart_path.read_text().splitlines(keepends=True)
        current_dep = None
        current_alias = None
        updated = False

        for i, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if stripped.startswith("- name:"):
                current_dep = stripped.split(":", 1)[1].strip()
                current_alias = None
            elif current_dep and stripped.startswith("alias:"):
                current_alias = stripped.split(":", 1)[1].strip()
            elif current_dep and stripped.startswith("version:"):
                match_key = current_alias or current_dep
                if match_key in version_updates:
                    new_ver = version_updates[match_key]
                    indent = raw_line[: len(raw_line) - len(raw_line.lstrip())]
                    # Preserve any inline comment on the version line
                    comment = ""
                    if "#" in raw_line:
                        comment = " " + raw_line[raw_line.index("#") :].rstrip("\n")
                    lines[i] = f"{indent}version: {new_ver}{comment}\n"
                    updated = True
                current_dep = None
                current_alias = None

        if updated:
            chart_path.write_text("".join(lines))
        return updated

    def calculate_total_prs(self, rollout_order):
        # 1 for dependencies update
        # 2 for stage (shards, core+agents)
        # 2*N for prod regions (each region: shards, then core+agents)
        # 1 for snowflake
        # 1 for customer dashboards
        self.total_prs = 1 + 2 + (2 * len(rollout_order["rolloutOrder"])) + 1 + 1
        return self.total_prs

    def _script_flags(self):
        """Extra CLI flags forwarded to helper scripts."""
        return ["--dry-run"] if self.dry_run else []

    def create_branch(self, branch_name, base_branch="master"):
        """Create and checkout a branch from master (clean state)"""
        # Always start from master to keep PRs isolated
        self.run_cmd(["git", "checkout", base_branch])
        self.run_cmd(["git", "pull", "origin", base_branch])
        if not self.dry_run:
            # Stash any local edits so reset --hard doesn't destroy workstation state,
            # then wipe to a clean origin baseline.
            self.run_cmd(
                ["git", "stash", "push", "--include-untracked", "-m", "pre-rollout-auto-stash"]
            )
            self.run_cmd(["git", "reset", "--hard", f"origin/{base_branch}"])

        # Check if branch already exists locally
        result = self.run_cmd(["git", "branch", "--list", branch_name], capture=True)
        if result and result.strip():
            logger.info(f"Branch {branch_name} already exists, deleting and recreating...")
            self.run_cmd(["git", "branch", "-D", branch_name])

        # Check if branch exists remotely
        result = self.run_cmd(["git", "ls-remote", "--heads", "origin", branch_name], capture=True)
        if result and result.strip():
            logger.info(f"Remote branch {branch_name} exists, will force push later")

        self.run_cmd(["git", "checkout", "-b", branch_name])
        self.branch_names.append(branch_name)

    def commit_and_push(self, message, paths=None):
        """Commit changes and push branch (skip if no changes)"""
        add_paths = paths or ["."]
        self.run_cmd(["git", "add", *add_paths])

        # Check if there are any changes to commit
        # git status --porcelain returns empty string if no changes
        if self.dry_run:
            status = "dummy changes for dry run"
        else:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout

        if not status or not status.strip():
            logger.warning("No changes to commit, skipping...")
            return False

        self.run_cmd(["git", "commit", "-m", message])
        if not self.dry_run:
            # Force push to handle branch updates
            self.run_cmd(["git", "push", "--force", "origin", self.branch_names[-1]])
        return True

    def create_pr(self, title, body, has_changes=True):
        """Create a PR using gh CLI (or update if exists)"""
        self.pr_number += 1
        pr_title = f"[{self.pr_number}/{self.total_prs}] {title}"

        if not has_changes:
            logger.info(f"Skipping PR creation (no changes): {pr_title}")
            return

        if self.dry_run:
            logger.info(f"[DRY RUN] Would create PR: {pr_title}")
            return

        # Check if an open PR already exists for this branch
        branch_name = self.branch_names[-1]
        check_cmd = ["gh", "pr", "view", branch_name, "--json", "state,number,url"]
        try:
            result = subprocess.run(check_cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0 and result.stdout:
                pr_info = json.loads(result.stdout)
                pr_state = pr_info.get("state", "").upper()
                pr_number = pr_info.get("number")

                if pr_state == "OPEN":
                    logger.info(
                        f"Open PR #{pr_number} exists for branch {branch_name}, updating..."
                    )
                    # Update the PR title and body
                    self.run_cmd(["gh", "pr", "edit", branch_name, "--title", pr_title])
                    self.run_cmd(["gh", "pr", "edit", branch_name, "--body", body])
                    logger.success(f"Updated existing PR: {pr_title}")
                    return
                elif pr_state == "CLOSED":
                    logger.info(
                        f"Closed PR #{pr_number} exists for branch {branch_name}, reopening..."
                    )
                    # Reopen the closed PR and update it
                    self.run_cmd(["gh", "pr", "reopen", str(pr_number)])
                    self.run_cmd(["gh", "pr", "edit", branch_name, "--title", pr_title])
                    self.run_cmd(["gh", "pr", "edit", branch_name, "--body", body])
                    logger.success(f"Reopened and updated PR: {pr_title}")
                    return
                elif pr_state == "MERGED":
                    logger.info(
                        f"Previous PR #{pr_number} for branch {branch_name} was merged, creating new PR for this cycle..."
                    )
                    # Fall through to create a new PR
        except Exception as e:
            logger.debug(f"No existing PR found for {branch_name}: {e}")
            pass  # PR doesn't exist, create it

        cmd = [
            "gh",
            "pr",
            "create",
            "--title",
            pr_title,
            "--body",
            body,
            "--base",
            "master",
        ]
        self.run_cmd(cmd)
        logger.success(f"Created PR: {pr_title}")

    def update_dependencies_and_images(self):
        """Step 1: Update helm dependencies and docker images for all charts"""
        branch_name = "update-deps-and-images"
        self.create_branch(branch_name)

        logger.info("Updating helm dependencies...")
        # Loop per-chart instead of --update-all to avoid the implicit
        # update_customer_dashboards() side-effect in update_helm_dependencies.py.
        for chart_name in CHARTS:
            self.run_cmd(
                [
                    "uv",
                    "run",
                    "--project",
                    ".github/scripts",
                    ".github/scripts/rollout/update_helm_dependencies.py",
                    "--chart",
                    chart_name,
                    "--download",
                    *self._script_flags(),
                ]
            )

        logger.info("Updating docker images...")
        self.run_cmd(
            [
                "uv",
                "run",
                "--project",
                ".github/scripts",
                ".github/scripts/rollout/update_docker_images.py",
                "--update-all",
                *self._script_flags(),
            ]
        )

        logger.info("Bumping chart versions...")
        self.run_cmd(
            [
                "uv",
                "run",
                "--project",
                ".github/scripts",
                ".github/scripts/rollout/bump_chart_version.py",
                "--all",
                "--patch",
                *self._script_flags(),
            ]
        )

        # Capture the bumped chart versions for use in subsequent PRs.
        # In dry-run mode bump_chart_version.py --dry-run does NOT write to disk, so
        # Chart.yaml still holds the pre-bump version.  Synthesise the would-be patch
        # bump in memory so downstream dry-run logs (update_config_files etc.) fire.
        logger.info("Capturing bumped chart versions...")
        for chart_name in CHARTS:
            chart_file = REPO_ROOT / f"alchemy-observability-{chart_name}" / "Chart.yaml"
            if chart_file.exists():
                with open(chart_file) as f:
                    chart_data = yaml.safe_load(f)
                    version = chart_data.get("version")
                    if self.dry_run and version:
                        parts = version.split(".")
                        parts[-1] = str(int(parts[-1]) + 1)
                        version = ".".join(parts)
                    self.chart_versions[chart_name] = version
                    logger.info(f"  {chart_name}: {self.chart_versions[chart_name]}")

        has_changes = self.commit_and_push(
            "chore: update dependencies and images for all charts",
            paths=ALCHEMY_CHART_DIRS,
        )
        self.create_pr(
            "Update chart dependencies and images",
            "Updates helm dependencies and docker images to latest versions.\n\n"
            "Changes:\n"
            "- Updated helm chart dependencies\n"
            "- Updated container images to latest patch versions\n"
            "- Bumped chart versions",
            has_changes=has_changes,
        )

    def capture_current_chart_versions(self, chart_names):
        """Load already-published wrapper versions for a stage-only rollout."""
        logger.info("Capturing current chart versions for stage rollout...")
        for chart_name in chart_names:
            chart_file = REPO_ROOT / f"alchemy-observability-{chart_name}" / "Chart.yaml"
            if not chart_file.exists():
                raise ValueError(f"Chart file not found for {chart_name}: {chart_file}")

            with open(chart_file) as f:
                version = yaml.safe_load(f).get("version")
            if not version:
                raise ValueError(f"Chart version missing from {chart_file}")

            self.chart_versions[chart_name] = str(version)
            logger.info(f"  {chart_name}: {self.chart_versions[chart_name]}")

    def determine_chart_type(self, config_file):
        """Determine chart type based on file path structure"""
        path_parts = config_file.parts

        # Check if it's in observability-platform (always shards)
        if "observability-platform" in path_parts:
            return "shard"

        # Check if it's in customer-dashboards (always grafana)
        if "customer-dashboards" in path_parts:
            return "grafana"

        # Check if it's in remote-clusters
        if "remote-clusters" in path_parts:
            # If directory name is "core", it's a core instance
            if config_file.parent.name == "core":
                return "core"
            # Otherwise it's an agent instance
            else:
                return "agent"

        return None

    def discover_regions(self, env):
        """Discover all regions that have config.yaml files for an environment."""
        regions = set()
        for config_file in REPO_ROOT.glob(f"helm/*/{env}/**/config.yaml"):
            # Path: helm/<category>/<env>/<region>/...
            rel = config_file.relative_to(REPO_ROOT)
            # parts: ('helm', category, env, region, ...)
            if len(rel.parts) >= 4:
                regions.add(rel.parts[3])
        return sorted(regions)

    def update_config_files(self, env, region, charts):
        """Update config.yaml files for specific environment/region and charts"""
        pattern = f"helm/*/{env}/{region}/**/config.yaml"
        config_files = list(REPO_ROOT.glob(pattern))

        for config_file in config_files:
            # Determine chart type based on path structure
            chart_type = self.determine_chart_type(config_file)

            # Only update config files for the specified charts
            if chart_type not in charts:
                continue

            with open(config_file) as f:
                config = yaml.safe_load(f)

            if "chartVersion" in config:
                current_version = str(config["chartVersion"])
                if ">=" in current_version or ">" in current_version:
                    logger.info(
                        f"Skipping {config_file.relative_to(REPO_ROOT)}: floating version {current_version!r}"
                    )
                    continue

                # Use the stored chart version from the first PR
                if chart_type in CHARTS and chart_type in self.chart_versions:
                    new_version = self.chart_versions[chart_type]
                    if new_version and current_version != new_version:
                        config["chartVersion"] = new_version
                        rel_path = config_file.relative_to(REPO_ROOT)
                        if self.dry_run:
                            logger.info(
                                f"[DRY RUN] Would update {rel_path}: {chart_type} → {new_version}"
                            )
                        else:
                            with open(config_file, "w") as f:
                                yaml.dump(
                                    config,
                                    f,
                                    default_flow_style=False,
                                    sort_keys=False,
                                )
                            logger.info(f"Updated {rel_path}: {chart_type} → {new_version}")

    def rollout_stage_shards(self):
        """Step 2: Rollout shards to stage environment"""
        branch_name = "rollout-stage-shards"
        self.create_branch(branch_name)

        stage_regions = self.discover_regions("stage")
        logger.info(f"Rolling out shards to stage regions: {stage_regions}")
        for region in stage_regions:
            self.update_config_files("stage", region, SHARD_CHARTS)

        has_changes = self.commit_and_push(
            "chore: rollout updated shards to stage environment",
            paths=["helm/"],
        )
        self.create_pr(
            "Rollout shards to stage",
            "Deploy updated shard chart versions to stage environment.\n\n"
            "All regions will be updated.",
            has_changes=has_changes,
        )

    def rollout_stage_core_agents(self, charts=None):
        """Step 3: Rollout the selected core and/or agent charts to stage."""
        branch_name = "rollout-stage-core-agents"
        self.create_branch(branch_name)

        selected_charts = charts or CORE_AGENT_CHARTS
        chart_description = " and ".join(selected_charts)

        stage_regions = self.discover_regions("stage")
        logger.info(f"Rolling out {', '.join(selected_charts)} to stage regions: {stage_regions}")
        for region in stage_regions:
            self.update_config_files("stage", region, selected_charts)

        has_changes = self.commit_and_push(
            "chore: rollout updated core+agents to stage environment",
            paths=["helm/"],
        )
        self.create_pr(
            f"Rollout {chart_description} to stage",
            f"Deploy updated {chart_description} chart versions to stage environment.\n\n"
            "All regions will be updated.",
            has_changes=has_changes,
        )

    def rollout_stage_only(self, chart_names):
        """Create only the stage rollout PRs for already-published chart versions."""
        selected_charts = set(chart_names)
        self.total_prs = int("shard" in selected_charts) + int(
            bool(selected_charts.intersection(CORE_AGENT_CHARTS))
        )
        self.capture_current_chart_versions(chart_names)
        self.preflight_chart_publication(chart_names)

        if "shard" in selected_charts:
            logger.info("Step 1: Rollout shards to stage")
            self.rollout_stage_shards()

        core_agent_charts = [chart for chart in CORE_AGENT_CHARTS if chart in selected_charts]
        if core_agent_charts:
            logger.info("Step 2: Rollout core/agents to stage")
            self.rollout_stage_core_agents(core_agent_charts)

        logger.success(f"Created {self.total_prs} stage rollout PR(s) successfully!")

    def rollout_prod_region_shards(self, regions):
        """Rollout shards to production regions"""
        region_str = "-".join(regions)
        branch_name = f"rollout-prod-shards-{region_str}"
        self.create_branch(branch_name)

        logger.info(f"Rolling out shards to prod: {', '.join(regions)}...")
        for region in regions:
            self.update_config_files("prod", region, SHARD_CHARTS)

        has_changes = self.commit_and_push(
            f"chore: rollout updated shards to prod: {', '.join(regions)}",
            paths=["helm/"],
        )
        self.create_pr(
            f"Rollout shards to prod: {', '.join(regions)}",
            f"Deploy updated shard chart versions to production regions: {', '.join(regions)}.\n\n"
            "Verify shard metrics and alerts after deployment.",
            has_changes=has_changes,
        )

    def rollout_prod_region_core_agents(self, regions):
        """Rollout core+agents to production regions"""
        region_str = "-".join(regions)
        branch_name = f"rollout-prod-core-agents-{region_str}"
        self.create_branch(branch_name)

        logger.info(f"Rolling out core+agents to prod: {', '.join(regions)}...")
        for region in regions:
            self.update_config_files("prod", region, CORE_AGENT_CHARTS)

        has_changes = self.commit_and_push(
            f"chore: rollout updated core+agents to prod: {', '.join(regions)}",
            paths=["helm/"],
        )
        self.create_pr(
            f"Rollout core+agents to prod: {', '.join(regions)}",
            f"Deploy updated core and agent chart versions to production regions: {', '.join(regions)}.\n\n"
            "Verify core and agent metrics and alerts after deployment.",
            has_changes=has_changes,
        )

    def update_snowflake(self):
        """Final step: Update snowflake production config"""
        branch_name = "update-snowflake-prod"
        self.create_branch(branch_name)

        logger.info("Updating snowflake Chart.yaml dependency versions...")
        snowflake_chart = (
            REPO_ROOT / "helm/remote-clusters/prod/use1/aws/observability/core/Chart.yaml"
        )

        pinned_deps = self.get_pinned_deps_from_chart(snowflake_chart)

        # Update all Chart.yaml dependencies (comment-preserving)
        if snowflake_chart.exists():
            with open(snowflake_chart) as f:
                chart_data = yaml.safe_load(f)

            version_updates = {}
            if "dependencies" in chart_data:
                for dep in chart_data["dependencies"]:
                    dep_name = dep.get("name")
                    dep_alias = dep.get("alias")
                    old_version = dep.get("version")
                    match_key = dep_alias or dep_name
                    if dep_name in pinned_deps or match_key in pinned_deps:
                        logger.info(f"Skipping pinned dependency {dep_name} ({old_version})")
                        continue

                    # Update alchemy-observability-core to the bumped version
                    if dep_name == "alchemy-observability-core" and "core" in self.chart_versions:
                        new_version = self.chart_versions["core"]
                        if old_version != new_version:
                            version_updates[match_key] = new_version
                            logger.info(f"Updated {dep_name}: {old_version} → {new_version}")

                    # Update other helm chart dependencies (thanos, prometheus-cloudwatch-exporter, etc)
                    elif dep_name and dep.get("repository"):
                        repo_url = dep.get("repository")
                        if repo_url and not repo_url.startswith("oci://"):
                            try:
                                temp_repo = f"temp-snowflake-{dep_name}"
                                self.run_cmd(["helm", "repo", "add", temp_repo, repo_url])
                                self.run_cmd(["helm", "repo", "update", temp_repo])

                                output = self.run_cmd(
                                    [
                                        "helm",
                                        "search",
                                        "repo",
                                        f"{temp_repo}/{dep_name}",
                                        "--output",
                                        "json",
                                    ],
                                    capture=True,
                                )
                                if output:
                                    results = json.loads(output)
                                    if results and len(results) > 0:
                                        latest_version = results[0]["version"]
                                        if latest_version != old_version:
                                            version_updates[match_key] = latest_version
                                            logger.info(
                                                f"Updated {dep_name}: {old_version} → {latest_version}"
                                            )

                                self.run_cmd(["helm", "repo", "remove", temp_repo])
                            except Exception as e:
                                logger.warning(f"Could not update {dep_name}: {e}")

            if version_updates:
                if self.dry_run:
                    for match_key, new_version in version_updates.items():
                        logger.info(
                            f"[DRY RUN] Would update {snowflake_chart}: {match_key} → {new_version}"
                        )
                else:
                    self.update_chart_versions_preserving_comments(snowflake_chart, version_updates)

        logger.info("Updating snowflake values.yaml docker images...")
        snowflake_values = "helm/remote-clusters/prod/use1/aws/observability/core/values.yaml"
        self.run_cmd(
            [
                "uv",
                "run",
                "--project",
                ".github/scripts",
                ".github/scripts/rollout/update_docker_images.py",
                "--chart",
                "core",
                "--values-file",
                snowflake_values,
                *self._script_flags(),
            ]
        )

        has_changes = self.commit_and_push(
            "chore: update snowflake: helm dependencies + docker images",
            paths=["helm/remote-clusters/prod/use1/aws/observability/core/"],
        )
        self.create_pr(
            "Update snowflake production config",
            "Update helm chart dependencies and container images in snowflake.\n\n"
            f"Updated:\n"
            f"- Chart.yaml helm dependencies (alchemy-observability-core, thanos, prometheus-cloudwatch-exporter)\n"
            f"- {snowflake_values} docker images",
            has_changes=has_changes,
        )

    def update_customer_dashboard_configs(self):
        """Update grafanaChartVersion and shardChartVersion in customer dashboard config.yaml files"""
        config_files = list(REPO_ROOT.glob("helm/customer-dashboards/**/config.yaml"))

        version_map = {
            "grafanaChartVersion": self.chart_versions.get("grafana"),
            "shardChartVersion": self.chart_versions.get("shard"),
        }

        for config_file in config_files:
            with open(config_file) as f:
                config = yaml.safe_load(f)

            updated = False
            for key, new_version in version_map.items():
                if new_version and key in config and config[key] != new_version:
                    config[key] = new_version
                    updated = True
                    rel_path = config_file.relative_to(REPO_ROOT)
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would update {rel_path}: {key} → {new_version}")
                    else:
                        logger.info(f"Updated {rel_path}: {key} → {new_version}")

            if updated and not self.dry_run:
                with open(config_file, "w") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    def rollout_customer_dashboards(self):
        """Step 13: Update customer dashboard chart versions"""
        branch_name = "rollout-customer-dashboards"
        self.create_branch(branch_name)

        logger.info("Updating customer dashboard chart versions...")
        self.update_customer_dashboard_configs()

        grafana_ver = self.chart_versions.get("grafana", "N/A")
        shard_ver = self.chart_versions.get("shard", "N/A")

        has_changes = self.commit_and_push(
            "chore: update customer dashboard chart versions",
            paths=["helm/customer-dashboards/"],
        )
        self.create_pr(
            "Update customer dashboards",
            "Update customer dashboard chart dependency versions.\n\n"
            "Changes:\n"
            f"- Updated alchemy-observability-grafana to {grafana_ver}\n"
            f"- Updated alchemy-observability-shard to {shard_ver}",
            has_changes=has_changes,
        )

    # ------------------------------------------------------------------ #
    #  Hotfix helpers                                                      #
    # ------------------------------------------------------------------ #

    def _chart_name_to_type(self, chart_name):
        """Return the short type (shard/core/agent/grafana) for an alchemy chart name.

        Exits non-zero if the name is not a known alchemy-observability-* chart.
        """
        prefix = "alchemy-observability-"
        if chart_name.startswith(prefix):
            chart_type = chart_name[len(prefix) :]
            if chart_type not in CHARTS:
                logger.error(
                    f"Unknown chart type {chart_type!r} derived from {chart_name!r}. "
                    f"Valid charts: {', '.join(CHARTS)}"
                )
                sys.exit(1)
            return chart_type
        logger.error(
            f"Chart name {chart_name!r} does not start with '{prefix}'. "
            f"Valid charts: {', '.join(f'{prefix}{c}' for c in CHARTS)}"
        )
        sys.exit(1)

    @staticmethod
    def _version_tuple(version_str):
        """Parse '0.0.21' into a comparable tuple. Raises ValueError on bad input."""
        try:
            return tuple(int(x) for x in str(version_str).split("."))
        except ValueError, AttributeError:
            raise ValueError(f"Cannot parse version: {version_str!r}")

    def _find_hotfix_targets(self, chart_type, environment="all"):
        """Return pinned configs for the requested chart and environment."""
        if environment not in {"all", "stage", "prod"}:
            raise ValueError(f"Unsupported hotfix environment: {environment!r}")

        # Map chart_type → (config_file_chart_type, field_name_in_config)
        type_field_map = {
            "shard": [("shard", "chartVersion"), ("grafana", "shardChartVersion")],
            "grafana": [("grafana", "grafanaChartVersion")],
            "core": [("core", "chartVersion")],
            "agent": [("agent", "chartVersion")],
        }

        targets = dict(type_field_map.get(chart_type, []))
        if not targets:
            return []

        results = []
        for config_file in sorted(REPO_ROOT.glob("helm/**/config.yaml")):
            relative_path = config_file.relative_to(REPO_ROOT)
            if environment != "all" and (
                len(relative_path.parts) < 3 or relative_path.parts[2] != environment
            ):
                continue
            ct = self.determine_chart_type(config_file)
            if ct not in targets:
                continue
            field_name = targets[ct]
            try:
                with open(config_file) as f:
                    config = yaml.safe_load(f)
            except yaml.YAMLError:
                continue
            if not config or field_name not in config:
                continue
            current_version = str(config[field_name])
            if ">=" in current_version or ">" in current_version:
                logger.info(
                    f"Skipping {config_file.relative_to(REPO_ROOT)}: "
                    f"floating version {current_version!r}"
                )
                continue
            results.append((config_file, current_version, field_name))
        return results

    def _authenticate_ghcr(self):
        """Authenticate Helm to GHCR once for this rollout invocation."""
        if self._ghcr_authenticated:
            return True

        ghcr_token = os.environ.get("GHCR_TOKEN", "").strip()
        if not ghcr_token:
            logger.error(
                "GHCR_TOKEN environment variable is not set or empty; "
                "set a token with read:packages before starting a rollout"
            )
            return False

        try:
            # Login to GHCR using the token via stdin (prevents token leakage in ps/proc).
            # GHCR only validates the token; username is ignored but required by helm CLI.
            login_result = subprocess.run(
                ["helm", "registry", "login", "-u", "username", "--password-stdin", "ghcr.io"],
                cwd=REPO_ROOT,
                input=ghcr_token,
                capture_output=True,
                text=True,
                check=False,
            )
            if login_result.returncode != 0:
                logger.error(
                    f"Failed to login to GHCR: {(login_result.stderr or login_result.stdout).strip()}"
                )
                return False
            logger.debug("Successfully authenticated to GHCR")
            self._ghcr_authenticated = True
            return True
        except FileNotFoundError:
            logger.error("'helm' binary not found — cannot login to GHCR")
            return False
        except subprocess.SubprocessError as exc:
            logger.error(f"Login to GHCR failed (subprocess error): {exc}")
            return False

    def _check_ghcr_published(self, chart_name, version):
        """Return whether an exact chart version is readable from GHCR."""
        if not self._authenticate_ghcr():
            return False

        oci_url = f"oci://ghcr.io/alchemy-docker/helm-charts/{chart_name}"
        try:
            result = subprocess.run(
                ["helm", "show", "chart", oci_url, "--version", version],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                logger.debug(
                    f"helm show chart exited {result.returncode}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )
                logger.error(
                    f"Chart {chart_name}:{version} not found or not accessible at {oci_url}"
                )
                return False
            return True
        except FileNotFoundError:
            logger.error("'helm' binary not found — cannot verify chart publication")
            return False
        except subprocess.SubprocessError as exc:
            logger.error(f"GHCR check failed (subprocess error): {exc}")
            return False

    def preflight_chart_publication(self, chart_types):
        """Fail before planning rollout changes when a requested chart is unreadable.

        Stage rollouts deploy versions already recorded in each wrapper Chart.yaml, so
        every requested version must exist and be readable from GHCR before branches
        are created or config files are changed.  This also runs for --dry-run: a plan
        that cannot fetch its charts is not actionable.
        """
        logger.info("Preflighting requested chart versions in GHCR...")
        unavailable = []
        for chart_type in chart_types:
            version = self.chart_versions.get(chart_type)
            if not version:
                raise ValueError(f"No captured version for chart type: {chart_type}")
            chart_name = f"alchemy-observability-{chart_type}"
            logger.info(f"  Checking {chart_name}:{version}")
            if not self._check_ghcr_published(chart_name, version):
                unavailable.append(f"{chart_name}:{version}")

        if unavailable:
            logger.error(
                "GHCR preflight failed; publish the chart(s) and verify read:packages access: "
                + ", ".join(unavailable)
            )
            sys.exit(1)

        logger.success("GHCR preflight passed for all requested chart versions.")

    @staticmethod
    def _update_config_field_in_place(config_file, field_name, new_version):
        """Rewrite a single top-level YAML field in a config.yaml, preserving all other
        content (comments, ordering, spacing).  Only the matching ``<field>: <value>``
        line is replaced; every other byte is kept verbatim.

        Returns True if the field was found and updated, False otherwise.
        """
        lines = config_file.read_text().splitlines(keepends=True)
        updated = False
        for i, raw_line in enumerate(lines):
            # Restrict to top-level keys: nested keys with the same name must not match.
            indent = raw_line[: len(raw_line) - len(raw_line.lstrip())]
            if indent:
                continue
            if raw_line.lstrip().startswith(f"{field_name}:"):
                # Preserve any inline comment on the value line
                comment = ""
                if "#" in raw_line:
                    comment = " " + raw_line[raw_line.index("#") :].rstrip("\n")
                lines[i] = f"{field_name}: {new_version}{comment}\n"
                updated = True
                break
        if updated:
            config_file.write_text("".join(lines))
        return updated

    def _ensure_hotfix_label(self):
        """Create the rollout/hotfix-override GitHub label if it does not exist."""
        label = "rollout/hotfix-override"
        check = subprocess.run(
            ["gh", "label", "list", "--limit", "200", "--json", "name"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode == 0 and check.stdout:
            try:
                existing = [lbl["name"] for lbl in json.loads(check.stdout)]
                if label in existing:
                    return
            except json.JSONDecodeError, KeyError:
                pass
        logger.info(f"Creating GitHub label: {label}")
        subprocess.run(
            [
                "gh",
                "label",
                "create",
                label,
                "--color",
                "B60205",
                "--description",
                "Emergency hotfix override for staged rollout gate",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def hotfix_rollout(self, chart_name, version_override=None, environment="all"):
        """Emergency roll-forward for pinned chart versions in one environment scope.

        Steps:
          1. Refuse if working tree is dirty or ahead of origin/master (non-dry-run only).
          2. Resolve target version (--target-version or Chart.yaml, after fetching origin/master).
          3. Locate every config.yaml that pins this chart in the requested environment.
          4. Abort if any pinned version >= target (prevents accidental downgrade).
          5. Verify the target version is published and readable from GHCR.
          6. In --dry-run mode: print the diff and return.
          7. Create branch hotfix-<chart>-<version>-<timestamp>, update files, commit.
          8. Open a single [HOTFIX] PR with label rollout/hotfix-override.
        """
        chart_type = self._chart_name_to_type(chart_name)
        environment_name = "all environments" if environment == "all" else environment
        environment_suffix = "" if environment == "all" else f"-{environment}"
        title_scope = "" if environment == "all" else f"[{environment.upper()}]"

        # Fetch origin/master to ensure version resolution uses a fresh ref
        if not self.dry_run:
            logger.info("Fetching origin/master to ensure ref is up to date...")
            subprocess.run(
                ["git", "fetch", "origin", "master", "--quiet"],
                cwd=REPO_ROOT,
                check=False,  # non-fatal; offline environments can still proceed
            )

        # Resolve target version
        if version_override:
            target_version = str(version_override)
        else:
            chart_yaml = REPO_ROOT / chart_name / "Chart.yaml"
            if not chart_yaml.exists():
                logger.error(f"Chart.yaml not found: {chart_yaml}")
                sys.exit(1)
            with open(chart_yaml) as f:
                data = yaml.safe_load(f)
            target_version = str(data["version"])

            # Warn if HEAD and origin/master have different versions (stale checkout).
            try:
                remote_yaml_text = subprocess.run(
                    ["git", "show", f"origin/master:{chart_name}/Chart.yaml"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                remote_version = str(yaml.safe_load(remote_yaml_text).get("version", ""))
                if remote_version and remote_version != target_version:
                    logger.warning(
                        f"HEAD {chart_name}/Chart.yaml has version {target_version!r} "
                        f"but origin/master has {remote_version!r}. "
                        "Run `git pull` to use the latest version, or pass --target-version explicitly."
                    )
            except subprocess.CalledProcessError:
                pass  # e.g., chart not on origin/master yet — ignore

        logger.info(f"Hotfix ({environment_name}): {chart_name} → {target_version}")

        # Find affected config files
        affected = self._find_hotfix_targets(chart_type, environment=environment)
        if not affected:
            logger.warning(
                f"No pinned config files found for chart type '{chart_type}' "
                f"in {environment_name}. Nothing to do."
            )
            return

        # Verify target > all current pinned versions
        broken_versions = set()
        try:
            target_vtuple = self._version_tuple(target_version)
        except ValueError as e:
            logger.error(f"Invalid target version: {e}")
            sys.exit(1)

        for config_file, current_version, field_name in affected:
            try:
                current_vtuple = self._version_tuple(current_version)
            except ValueError as e:
                logger.error(f"Cannot parse version in {config_file.relative_to(REPO_ROOT)}: {e}")
                sys.exit(1)
            if current_vtuple >= target_vtuple:
                logger.error(
                    f"ABORT: {config_file.relative_to(REPO_ROOT)} has "
                    f"{field_name}={current_version!r}, which is >= target {target_version!r}."
                )
                logger.error(
                    "Hotfix would not advance this deployment. "
                    "Use --target-version to specify a strictly greater version."
                )
                sys.exit(1)
            broken_versions.add(current_version)

        broken_sorted = sorted(broken_versions, key=self._version_tuple)

        # Preflight publication before the dry-run output too: an unreachable chart
        # makes a rollout plan non-actionable and must fail before branch creation.
        self.chart_versions[chart_type] = target_version
        self.preflight_chart_publication([chart_type])

        # Dry-run: print what would change and exit.
        if self.dry_run:
            logger.info("[DRY RUN] Changes that would be applied:")
            logger.info(
                f"  Branch:  hotfix-{chart_name}-{target_version}{environment_suffix}-<timestamp>"
            )
            logger.info(f"  PR title: [HOTFIX]{title_scope} {chart_name} → {target_version}")
            logger.info("  Label:   rollout/hotfix-override")
            logger.info(f"  Environment: {environment_name}")
            logger.info(f"  Broken version(s) being replaced: {broken_sorted}")
            logger.info(f"  Files to update ({len(affected)}):")
            for config_file, current_version, field_name in affected:
                rel = config_file.relative_to(REPO_ROOT)
                logger.info(f"    {rel}: {field_name}: {current_version!r} → {target_version!r}")
            return

        # Create branch
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        branch_name = f"hotfix-{chart_name}-{target_version}{environment_suffix}-{timestamp}"
        self.create_branch(branch_name)

        # Update config files (comment-preserving in-place rewrite — no yaml.dump).
        # We intentionally do NOT call regenerate_helm_values() here: ArgoCD's
        # helm-render-obs CMP regenerates Chart.yaml/values.yaml at sync time (see
        # CLAUDE.md), and skipping the regenerate step keeps the hotfix diff small
        # and reviewable under incident pressure.
        for config_file, current_version, field_name in affected:
            updated = self._update_config_field_in_place(config_file, field_name, target_version)
            assert updated, (
                f"Field {field_name!r} expected in {config_file.relative_to(REPO_ROOT)} but "
                "line-walker did not match — _find_hotfix_targets and "
                "_update_config_field_in_place have diverged."
            )
            logger.info(
                f"Updated {config_file.relative_to(REPO_ROOT)}: "
                f"{field_name}: {current_version!r} → {target_version!r}"
            )

        has_changes = self.commit_and_push(
            f"chore: hotfix {chart_type} chart to {target_version} in {environment_name}",
            paths=["helm/"],
        )
        if not has_changes:
            logger.warning("No changes detected after updating config files. Nothing to commit.")
            return

        # Ensure label exists and build PR body
        self._ensure_hotfix_label()

        affected_list = "\n".join(
            f"- `{cf.relative_to(REPO_ROOT)}` (`{fn}`: `{cv}` → `{target_version}`)"
            for cf, cv, fn in affected
        )
        pr_body = (
            f"## Emergency Roll-Forward Hotfix\n\n"
            f"**Chart:** `{chart_name}`  \n"
            f"**Target version:** `{target_version}`  \n"
            f"**Environment:** `{environment_name}`  \n"
            f"**Broken version(s) being replaced:** "
            f"{', '.join(f'`{v}`' for v in broken_sorted)}\n\n"
            f"## Affected Config Files ({len(affected)})\n\n"
            f"{affected_list}\n\n"
            f"## Pre-Merge Checklist\n\n"
            f"> **IMPORTANT:** Verify live ArgoCD state before merging:\n"
            f"> ```\n"
            f"> argocd app list | grep -E 'alchemy-observability-{chart_type}\\b'\n"
            f"> ```\n"
            f"> Confirm the version currently running matches the broken version above.\n"
            f"> Do not assume — running state may differ from last-committed config.\n\n"
            f"- [ ] Live ArgoCD state verified (running version matches broken version above)\n"
            f"- [ ] Fix is confirmed merged to master\n"
            f"- [ ] `{chart_name}:{target_version}` confirmed published to GHCR\n"
            f"- [ ] CI passes on this PR\n"
        )

        self.run_cmd(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"[HOTFIX]{title_scope} {chart_name} → {target_version}",
                "--body",
                pr_body,
                "--base",
                "master",
                "--label",
                "rollout/hotfix-override",
            ]
        )
        logger.success(f"Created hotfix PR: [HOTFIX]{title_scope} {chart_name} → {target_version}")
        logger.info(f"Branch: {branch_name}")
        logger.info("Merge after CI passes; ArgoCD will auto-sync on merge.")


def main():
    parser = argparse.ArgumentParser(
        description="Create dependency update PRs",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating PRs")
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Create only stage rollout PRs using the current wrapper chart versions",
    )
    parser.add_argument(
        "--stage-charts",
        default="core,agent,shard",
        help="Comma-separated charts for --stage-only: core, agent, shard (default: all three)",
    )
    parser.add_argument(
        "--hotfix",
        action="store_true",
        help="Emergency roll-forward mode: update all pinned chartVersions for a chart in one PR",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Fast rollout: collapse all prod regions into one wave "
            "(stage → all prod in parallel) instead of the default 3 sequential prod waves. "
            "Creates 7 PRs instead of 11."
        ),
    )
    parser.add_argument(
        "--chart",
        default="alchemy-observability-shard",
        help="Chart name for --hotfix (default: alchemy-observability-shard)",
    )
    parser.add_argument(
        "--target-version",
        default=None,
        help="Target version for --hotfix (default: read from CHART/Chart.yaml)",
    )
    parser.add_argument(
        "--environment",
        choices=("all", "stage", "prod"),
        default="all",
        help="Environment scope for --hotfix (default: all)",
    )
    args = parser.parse_args()

    # Guard: stage-only, hotfix, and fast rollouts are mutually exclusive.
    if sum((args.hotfix, args.fast, args.stage_only)) > 1:
        parser.error("--stage-only, --hotfix, and --fast are mutually exclusive")

    # Guard: hotfix-only flags error early if misused.
    _default_chart = "alchemy-observability-shard"
    if not args.hotfix and (
        args.chart != _default_chart or args.target_version is not None or args.environment != "all"
    ):
        parser.error(
            "--chart, --target-version, and --environment are only valid "
            "when --hotfix is also specified"
        )
    if not args.stage_only and args.stage_charts != "core,agent,shard":
        parser.error("--stage-charts is only valid when --stage-only is also specified")

    creator = PRCreator(dry_run=args.dry_run)

    if args.hotfix:
        creator.hotfix_rollout(
            chart_name=args.chart,
            version_override=args.target_version,
            environment=args.environment,
        )
        return

    if args.stage_only:
        requested_charts = [
            chart.strip() for chart in args.stage_charts.split(",") if chart.strip()
        ]
        allowed_charts = set(CORE_AGENT_CHARTS + SHARD_CHARTS)
        invalid_charts = sorted(set(requested_charts) - allowed_charts)
        if invalid_charts:
            parser.error(
                f"--stage-charts contains unsupported chart(s): {', '.join(invalid_charts)}"
            )
        if not requested_charts:
            parser.error("--stage-charts must name at least one chart")

        creator.rollout_stage_only(requested_charts)
        return

    if args.fast:
        # Fast rollout: all prod regions in a single wave (stage → all prod at once).
        all_prod_regions = sorted(creator.discover_regions("prod"))
        prod_waves = [all_prod_regions]
        logger.info(f"Fast rollout: all prod regions in one wave: {all_prod_regions}")
    else:
        # Standard rollout: explicit prod waves (order matters: canary → broader → primary)
        prod_waves = [["usw2"], ["euc1", "euc2", "apse1"], ["use1"]]

        # Discover any prod regions not covered by explicit waves and append as a final wave
        explicit_regions = {r for wave in prod_waves for r in wave}
        extra_regions = sorted(set(creator.discover_regions("prod")) - explicit_regions)
        if extra_regions:
            prod_waves.append(extra_regions)
            logger.info(
                f"Discovered additional prod regions not in explicit waves: {extra_regions}"
            )

    rollout_order = {
        "rolloutOrder": prod_waves,
        "environments": ["stage", "prod"],
    }

    total = creator.calculate_total_prs(rollout_order)

    logger.info(f"Will create {total} PRs")
    logger.info("=" * 80)

    try:
        # Step 1: Update dependencies and images
        logger.info("Step 1: Update dependencies and images")
        creator.update_dependencies_and_images()

        # Step 2: Rollout shards to stage
        logger.info("\nStep 2: Rollout shards to stage")
        creator.rollout_stage_shards()

        # Step 3: Rollout core+agents to stage
        logger.info("\nStep 3: Rollout core+agents to stage")
        creator.rollout_stage_core_agents()

        # Steps 4-9: Rollout to prod regions (shards first, then core+agents)
        step = 4
        for regions in rollout_order["rolloutOrder"]:
            logger.info(f"\nStep {step}: Rollout shards to prod: {regions}")
            creator.rollout_prod_region_shards(regions)
            step += 1

            logger.info(f"\nStep {step}: Rollout core+agents to prod: {regions}")
            creator.rollout_prod_region_core_agents(regions)
            step += 1

        # Step 10: Update snowflake
        logger.info(f"\nStep {step}: Update snowflake config")
        creator.update_snowflake()
        step += 1

        # Step 11: Update customer dashboards
        logger.info(f"\nStep {step}: Update customer dashboards")
        creator.rollout_customer_dashboards()

        logger.info("=" * 80)
        logger.success(f"Created {total} PRs successfully!")
        logger.info("\nMerge PRs in order:")
        for i, branch in enumerate(creator.branch_names, 1):
            logger.info(f"  {i}. {branch}")

    except Exception as e:
        logger.error("=" * 80)
        logger.error(f"Rollout failed at step {creator.pr_number + 1}/{total} due to {e}")
        if creator.branch_names:
            logger.error(f"\n{len(creator.branch_names)} PR(s) already created:")
            for i, branch in enumerate(creator.branch_names, 1):
                logger.error(f"  {i}. {branch}")
            logger.error("\nClean up these branches and PRs before retrying.")
        raise


if __name__ == "__main__":
    main()
