#!/usr/bin/env python3
"""Detect rollout PRs that pin a chart version stale relative to master.

Background — SEV2 (May 2026): a rollout PR pinned alchemy-observability-shard to
chart version 0.0.18, but the destructive-retentionSize fix that motivated the
rollout was only on master at 0.0.19 (still in-flight, not yet published). The
0.0.18 pin shipped fleet-wide, skipping the fix.

This check inspects each helm/*/config.yaml changed by the PR and compares the
pinned `chartVersion:` against `alchemy-observability-<type>/Chart.yaml` on the
base ref. Ahead pins (pinned > base) emit a warning (pre-publication scenario).
For behind pins (pinned < base): if the base version is already published in
GHCR the check emits a warning (normal in-progress rollout — a subsequent
rollout will deliver the newer version); if the base version is NOT yet
published the check fails — that is the SEV2 scenario where deploying the older
pin would skip unpublished master changes with no path to production.

Exemptions:
- floating chartVersion (">=...", ">...") — intentional on stage.
- any helm/<category>/stage/... path — stage releases are upgraded in-place.

Usage:
    uv run --project .github/scripts python .github/scripts/ci/check_rollout_chart_versions.py \
        --base-ref origin/master
"""

import argparse
import subprocess
import sys
from pathlib import Path

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.version_gate_findings import VersionGateFinding, append_findings, gate_crash_finding

GHCR_REGISTRY = "oci://ghcr.io/alchemy-docker/helm-charts"

CHART_DIR = {
    "core": "alchemy-observability-core",
    "agent": "alchemy-observability-agent",
    "shard": "alchemy-observability-shard",
    "grafana": "alchemy-observability-grafana",
}


def chart_type_for_config(config_path: Path) -> str | None:
    parts = config_path.parts
    if "observability-platform" in parts:
        return "shard"
    if "customer-dashboards" in parts:
        return "grafana"
    if "remote-clusters" in parts:
        # helm/remote-clusters/<env>/<region>/<provider>/<name>/{core|<agent-name>}/config.yaml
        return "core" if config_path.parent.name == "core" else "agent"
    return None


def is_stage_path(config_path: Path) -> bool:
    parts = config_path.parts
    try:
        helm_idx = parts.index("helm")
    except ValueError:
        return False
    # parts after helm: <category>/<env>/...
    return len(parts) > helm_idx + 2 and parts[helm_idx + 2] == "stage"


def parse_semver(v: str) -> tuple[int, int, int] | None:
    try:
        major, minor, patch = (int(p) for p in v.split("."))
    except ValueError:
        return None
    return major, minor, patch


def git_show(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def git_changed_configs(base_ref: str) -> list[Path]:
    """Files added/modified/renamed between base_ref and HEAD that are helm/*/config.yaml."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        Path(line)
        for line in out.splitlines()
        if line.startswith("helm/") and line.endswith("/config.yaml")
    ]


_ghcr_cache: dict[tuple[str, str], bool] = {}


def is_chart_published(chart_dir: str, version: str, registry: str) -> bool:
    """Return True if this exact chart version exists in the OCI registry.

    Results are cached per (chart_dir, version) pair. Returns False on any
    registry error so the caller can fail closed — a transient network hiccup
    should not silently promote an otherwise-stale pin.
    """
    key = (chart_dir, version)
    if key not in _ghcr_cache:
        result = subprocess.run(
            ["helm", "show", "chart", f"{registry}/{chart_dir}", "--version", version],
            capture_output=True,
            text=True,
            check=False,
        )
        _ghcr_cache[key] = result.returncode == 0
        logger.debug(
            f"GHCR {registry}/{chart_dir}@{version}: "
            f"{'published' if _ghcr_cache[key] else 'not found / error'}"
        )
    return _ghcr_cache[key]


def run_check(base_ref: str, ghcr_registry: str) -> tuple[int, list[VersionGateFinding]]:
    findings: list[VersionGateFinding] = []
    changed = git_changed_configs(base_ref)
    if not changed:
        logger.info("No helm/*/config.yaml files changed; nothing to validate.")
        return 0, findings

    base_versions: dict[str, str] = {}
    for chart_type, chart_dir in CHART_DIR.items():
        raw = git_show(base_ref, f"{chart_dir}/Chart.yaml")
        if raw is None:
            print(
                f"::warning::could not read {chart_dir}/Chart.yaml on {base_ref}; "
                f"skipping checks for chart type {chart_type!r}"
            )
            continue
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            print(
                f"::warning::malformed YAML in {chart_dir}/Chart.yaml on "
                f"{base_ref} ({exc}); skipping checks for chart type "
                f"{chart_type!r}"
            )
            continue
        v = data.get("version")
        if v:
            base_versions[chart_type] = str(v)

    failed = 0
    checked = 0
    for cfg in changed:
        chart_type = chart_type_for_config(cfg)
        if chart_type is None:
            continue
        if is_stage_path(cfg):
            logger.info(f"skip stage path (in-place overrides allowed): {cfg}")
            continue
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
        except FileNotFoundError:
            continue
        except yaml.YAMLError as exc:
            print(f"::error file={cfg}::malformed YAML ({exc}); skipping")
            continue
        pinned = data.get("chartVersion")
        if pinned is None:
            continue
        pinned_str = str(pinned).strip()
        if pinned_str.startswith(">"):
            logger.info(f"skip floating chartVersion {pinned_str!r}: {cfg}")
            continue

        base_raw = git_show(base_ref, str(cfg))
        if base_raw is not None:
            try:
                base_data = yaml.safe_load(base_raw) or {}
            except yaml.YAMLError as exc:
                print(
                    f"::warning file={cfg}::malformed YAML on {base_ref} "
                    f"({exc}); validating pin against base chart anyway"
                )
                base_data = {}
            base_pinned = base_data.get("chartVersion")
            if base_pinned is not None and str(base_pinned).strip() == pinned_str:
                continue

        base_v = base_versions.get(chart_type)
        if base_v is None:
            print(
                f"::warning file={cfg}::no base ref version known for chart "
                f"{chart_type!r}; skipping"
            )
            continue

        pinned_tuple = parse_semver(pinned_str)
        base_tuple = parse_semver(base_v)
        if pinned_tuple is None or base_tuple is None:
            print(
                f"::warning file={cfg}::unparseable semver "
                f"(pinned={pinned_str!r}, base={base_v!r}); skipping"
            )
            continue

        checked += 1
        chart_dir = CHART_DIR[chart_type]
        if pinned_tuple < base_tuple:
            if is_chart_published(chart_dir, base_v, ghcr_registry):
                print(
                    f"::warning file={cfg}::mid-rollout pin: chartVersion "
                    f"{pinned_str} is behind {chart_dir}/Chart.yaml on "
                    f"{base_ref} ({base_v}), but {base_v} is already "
                    f"published — a subsequent rollout will deliver it. "
                    f"Expected during an in-progress sequential rollout."
                )
            else:
                summary = (
                    f"Pins chartVersion {pinned_str} but {chart_dir}/Chart.yaml on "
                    f"{base_ref} is already at {base_v}, which has not been published "
                    f"yet. Deploying {pinned_str} would skip unpublished changes on master."
                )
                finding = VersionGateFinding(
                    check="stale-rollout-pin",
                    title=f"Stale chartVersion pin in `{cfg}`",
                    file=str(cfg),
                    summary=summary,
                    fix_steps=[
                        "Rebase onto latest master: `git fetch origin && git rebase origin/master`.",
                        f"Update `chartVersion:` in `{cfg}` to `{base_v}` "
                        f"(matches `{chart_dir}/Chart.yaml` on {base_ref}).",
                        f"Confirm the chart is published: "
                        f"`helm show chart {ghcr_registry}/{chart_dir} --version {base_v}`",
                        "If that command fails, merge/publish the upstream chart upgrade PR "
                        f"first, then update this rollout pin to `{base_v}`.",
                        "Do not pin a version older than master when master contains "
                        "unpublished fixes — that was the root cause of the May 2026 SEV2.",
                    ],
                )
                finding.emit_annotation()
                findings.append(finding)
                failed += 1
        elif pinned_tuple > base_tuple:
            print(
                f"::warning file={cfg}::pinned chartVersion {pinned_str} is "
                f"ahead of {chart_dir}/Chart.yaml on {base_ref} ({base_v}). "
                f"Unusual but not necessarily wrong — pre-publication PRs land "
                f"in this state. Verify the source chart is bumped + published "
                f"before this PR merges."
            )
        else:
            logger.info(f"ok: {cfg} pins {chart_type}@{pinned_str} (matches base)")

    logger.info(f"validated {checked} pinned chartVersion(s); {failed} stale")
    if failed:
        print(f"::error::found {failed} stale chartVersion pin(s) — see annotations above")
    return (1 if failed else 0), findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default="origin/master",
        help="Base ref to compare against (default: origin/master).",
    )
    parser.add_argument(
        "--ghcr-registry",
        default=GHCR_REGISTRY,
        help="OCI registry base URL for published charts (default: %(default)s).",
    )
    parser.add_argument(
        "--findings-file",
        type=Path,
        default=None,
        help="Append structured findings as JSONL for the PR comment step.",
    )
    args = parser.parse_args()

    try:
        code, findings = run_check(args.base_ref, args.ghcr_registry)
    except Exception as exc:
        logger.exception("stale-rollout-pin gate crashed unexpectedly")
        finding = gate_crash_finding("stale-rollout-pin", exc)
        finding.emit_annotation()
        append_findings(args.findings_file, [finding])
        return 1

    append_findings(args.findings_file, findings)
    return code


if __name__ == "__main__":
    sys.exit(main())
