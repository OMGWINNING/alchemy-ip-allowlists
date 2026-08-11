#! /usr/bin/env python3
"""Bump Helm Chart Version

Usage:
    .github/scripts/rollout/bump_chart_version.py --chart core --patch|--minor|--major [--dry-run]
    .github/scripts/rollout/bump_chart_version.py --all --patch [--dry-run]
    .github/scripts/rollout/bump_chart_version.py --chart core --show

The publish workflow runs once per merge to master, so a single PR should
produce exactly one version bump. If this branch has already bumped a chart
ahead of origin/master, this script refuses a second bump unless --force is
passed. Bot-generated rollout PRs branch from master and never re-enter, so
the guard is a no-op for them.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import CHARTS, BaseChartManager, add_common_arguments, get_repo_root

BASE_REF = "origin/master"


def _master_version(chart_dir):
    """Read the chart's version on origin/master via `git show`. Returns
    None when the ref isn't available (e.g. shallow clone in some CI paths)
    so the guard fails open rather than blocking legitimate work.

    Parses with yaml.safe_load to mirror get_version() (used for the local
    side of the comparison): a string match on `version:` would keep any
    surrounding quotes, so `version: "0.0.22"` on master would compare
    unequal to the unquoted `0.0.22` get_version() returns and fail open
    into a spurious 'already bumped' error."""
    try:
        out = subprocess.run(
            ["git", "show", f"{BASE_REF}:{chart_dir}/Chart.yaml"],
            cwd=get_repo_root(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    data = yaml.safe_load(out) or {}
    version = data.get("version")
    return str(version) if version is not None else None


class VersionBumper(BaseChartManager):
    def bump(self, chart_name, bump_type, force=False):
        current = self.get_version(chart_name)
        master = _master_version(CHARTS[chart_name])

        if not force and master and current != master:
            # Warn-and-skip rather than aborting: in --all mode a hard exit here
            # would skip every chart later in iteration order that still needs a
            # bump. main() turns a skip into exit 2 only for an explicit single
            # --chart request, preserving the "don't silently double-bump" signal.
            logger.warning(
                f"{chart_name}: skipping bump — already ahead of {BASE_REF} "
                f"({BASE_REF}={master}, current={current}). "
                f"Re-bumping would publish an extra version on merge. "
                f"Pass --force to override (rare: e.g. major bump after minor)."
            )
            return None

        data = self.read_chart(chart_name)
        major, minor, patch = map(int, current.split("."))

        if bump_type == "major":
            major, minor, patch = major + 1, 0, 0
        elif bump_type == "minor":
            minor, patch = minor + 1, 0
        else:  # patch
            patch += 1

        new_version = f"{major}.{minor}.{patch}"
        logger.success(f"{chart_name}: {current} → {new_version}")

        data["version"] = new_version
        self.write_chart(chart_name, data)
        return new_version


def main():
    parser = argparse.ArgumentParser(
        description="Bump chart version",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_arguments(parser)
    parser.add_argument("--all", action="store_true", help="Update all charts")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bump even if the chart is already ahead of origin/master",
    )

    bump = parser.add_mutually_exclusive_group()
    bump.add_argument("--major", action="store_true", help="Bump major version")
    bump.add_argument("--minor", action="store_true", help="Bump minor version")
    bump.add_argument("--patch", action="store_true", help="Bump patch version")
    bump.add_argument("--show", action="store_true", help="Show current versions")

    args = parser.parse_args()

    bumper = VersionBumper(dry_run=args.dry_run)
    charts = list(CHARTS.keys()) if args.all else ([args.chart] if args.chart else [])

    if not charts:
        parser.print_help()
        sys.exit(1)

    # Show mode
    if args.show:
        for chart in charts:
            logger.info(f"{chart}: {bumper.get_version(chart)}")
        return

    # Bump mode
    bump_type = (
        "major" if args.major else ("minor" if args.minor else ("patch" if args.patch else None))
    )
    if not bump_type:
        logger.error("Specify --major, --minor, or --patch")
        sys.exit(1)

    skipped = [chart for chart in charts if bumper.bump(chart, bump_type, force=args.force) is None]

    # An explicit single-chart bump that no-op'd (already ahead of master) is an
    # error worth surfacing; in --all mode the skip is benign (other charts still
    # bumped), so don't fail the whole run.
    if skipped and not args.all:
        sys.exit(2)

    logger.success("Done!")


if __name__ == "__main__":
    main()
