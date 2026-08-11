"""Common utilities for chart maintenance scripts"""

import pathlib
import subprocess
import yaml
from loguru import logger

# Chart configuration
CHARTS = {
    "core": "alchemy-observability-core",
    "agent": "alchemy-observability-agent",
    "shard": "alchemy-observability-shard",
    "grafana": "alchemy-observability-grafana",
}


def get_repo_root():
    """Get the repository root directory."""
    path = pathlib.Path(__file__).resolve()
    for candidate in path.parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"Could not find repository root from {path}")


class BaseChartManager:
    """Base class for chart management operations"""

    def __init__(self, repo_root=None, dry_run=False):
        self.repo_root = repo_root or get_repo_root()
        self.dry_run = dry_run

    def run_cmd(self, cmd, cwd=None, capture=False):
        """Run a command with optional dry-run mode"""
        if self.dry_run and cmd[0] not in ["helm"]:
            logger.info(f"[DRY RUN] {' '.join(cmd)}")
            return None
        result = subprocess.run(
            cmd,
            cwd=cwd or self.repo_root,
            capture_output=capture,
            text=True,
            check=True,
        )
        return result.stdout if capture else None

    def get_chart_path(self, chart_name):
        """Get the path to a chart directory"""
        return self.repo_root / CHARTS[chart_name]

    def get_chart_file(self, chart_name):
        """Get the path to a Chart.yaml file"""
        return self.get_chart_path(chart_name) / "Chart.yaml"

    def read_chart(self, chart_name):
        """Read and parse a Chart.yaml file"""
        chart_file = self.get_chart_file(chart_name)
        with open(chart_file) as f:
            return yaml.safe_load(f)

    def write_chart(self, chart_name, data):
        """Write a Chart.yaml file"""
        chart_file = self.get_chart_file(chart_name)
        if self.dry_run:
            logger.info(f"[DRY RUN] Would write {chart_file}")
            return
        with open(chart_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def get_version(self, chart_name):
        """Get the current version of a chart"""
        data = self.read_chart(chart_name)
        return data.get("version", "0.0.0")


def add_common_arguments(parser):
    """Add common command-line arguments to an ArgumentParser"""
    parser.add_argument(
        "--chart",
        choices=["core", "agent", "shard", "grafana"],
        help="Specific chart to operate on",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    return parser
