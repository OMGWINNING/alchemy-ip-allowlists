#!/usr/bin/env python3
"""Require a chart version bump when modifying alchemy-observability-* in prod use.

When a PR changes render-affecting content under
`alchemy-observability-{core,agent,shard,grafana}/` without bumping that chart's
`version:` in Chart.yaml, and the unchanged version is pinned on at least one
`helm/*/prod/**/config.yaml` on the base ref, the check fails. Render-affecting
paths are `templates/`, `values.yaml`, `files/`, `patches/`, `crds/`, and
`Chart.yaml` (dependency bumps, etc.). Docs-only edits (README, `*.md`,
`.helmignore`) do not trigger the gate.

Versions not yet referenced by any prod pin may still be overwritten in-place (the
normal pre-rollout / in-flight publish path).

Usage:
    uv run --project .github/scripts python \\
        .github/scripts/ci/check_alchemy_prod_version_bump.py --base-ref origin/master
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ci.check_prod_stage_alignment import chart_type_for_config, is_concrete_pin, is_prod_path
from lib.version_gate_findings import VersionGateFinding, append_findings, gate_crash_finding

CHART_DIR_TO_TYPE = {
    "alchemy-observability-core": "core",
    "alchemy-observability-agent": "agent",
    "alchemy-observability-shard": "shard",
    "alchemy-observability-grafana": "grafana",
}

RENDER_AFFECTING_DIR_NAMES = frozenset({"templates", "files", "patches", "crds"})
RENDER_AFFECTING_ROOT_FILES = frozenset({"values.yaml", "Chart.yaml"})


class GitError(RuntimeError):
    """Raised when a git invocation the gate depends on fails."""


def chart_dir_for_path(path: Path) -> str | None:
    """Return the alchemy-observability-* directory name if path is under one."""
    if not path.parts:
        return None
    top = path.parts[0]
    return top if top in CHART_DIR_TO_TYPE else None


def is_render_affecting_chart_path(path: Path) -> bool:
    """True when a changed file can alter rendered chart output."""
    chart_dir = chart_dir_for_path(path)
    if chart_dir is None:
        return False
    rel = path.relative_to(chart_dir)
    if len(rel.parts) == 1:
        return rel.parts[0] in RENDER_AFFECTING_ROOT_FILES
    return rel.parts[0] in RENDER_AFFECTING_DIR_NAMES


def git_show(ref: str, path: str, repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        return None
    return result.stdout


def git_changed_files(base_ref: str, repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise GitError(
            f"git diff against {base_ref} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return [Path(line) for line in result.stdout.splitlines() if line]


def read_chart_version_from_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return None
    version = data.get("version")
    return None if version is None else str(version).strip()


def read_chart_version(ref: str, chart_dir: str, repo_root: Path) -> str | None:
    return read_chart_version_from_text(git_show(ref, f"{chart_dir}/Chart.yaml", repo_root))


def list_prod_configs(base_ref: str, repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base_ref, "--", "helm"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise GitError(
            f"git ls-tree against {base_ref} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return [
        line
        for line in result.stdout.splitlines()
        if line.endswith("/config.yaml") and "/prod/" in f"/{line}/"
    ]


def pinned_version_from_config(data: dict, chart_type: str) -> str | None:
    """Return the config field that pins an alchemy chart version for chart_type."""
    if chart_type == "grafana":
        v = data.get("grafanaChartVersion", data.get("chartVersion"))
    else:
        v = data.get("chartVersion")
    return None if v is None else str(v).strip()


def collect_prod_pins(base_ref: str, repo_root: Path) -> dict[str, set[str]]:
    """Return chart_type -> set of concrete chartVersion pins on prod (base ref)."""
    prod_pins: dict[str, set[str]] = {}
    for rel_path in list_prod_configs(base_ref, repo_root):
        path = Path(rel_path)
        if not is_prod_path(path):
            continue
        chart_type = chart_type_for_config(path)
        if chart_type is None:
            continue
        raw = git_show(base_ref, rel_path, repo_root)
        if raw is None:
            continue
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            print(f"::warning file={rel_path}::malformed YAML on {base_ref} ({exc}); skipping")
            continue
        pinned_str = pinned_version_from_config(data, chart_type)
        if pinned_str is None or not is_concrete_pin(pinned_str):
            continue
        prod_pins.setdefault(chart_type, set()).add(pinned_str)
    return prod_pins


def changed_alchemy_chart_dirs(changed: list[Path]) -> set[str]:
    return {chart_dir_for_path(path) for path in changed if is_render_affecting_chart_path(path)}


def next_patch_version(version: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def run_check(base_ref: str, repo_root: Path) -> tuple[int, list[VersionGateFinding]]:
    findings: list[VersionGateFinding] = []
    changed = git_changed_files(base_ref, repo_root)
    if not changed:
        logger.info("No changed files vs base ref; nothing to validate.")
        return 0, findings

    touched = changed_alchemy_chart_dirs(changed)
    if not touched:
        logger.info("No render-affecting alchemy-observability-* chart files changed; skipping.")
        return 0, findings

    prod_pins = collect_prod_pins(base_ref, repo_root)
    for chart_type, pins in sorted(prod_pins.items()):
        logger.info(f"prod pins for chart_type={chart_type!r}: {sorted(pins)}")

    failed = 0
    checked = 0
    for chart_dir in sorted(touched):
        chart_type = CHART_DIR_TO_TYPE[chart_dir]
        base_version = read_chart_version(base_ref, chart_dir, repo_root)
        head_version = read_chart_version("HEAD", chart_dir, repo_root)

        if base_version is None:
            # Fail-open: unreadable base Chart.yaml is pathological, not a routine
            # rollout mistake (contrast check_prod_stage_alignment's git fail-closed).
            print(f"::warning::{chart_dir}: could not read Chart.yaml on {base_ref}; skipping")
            continue
        if head_version is None:
            print(f"::warning::{chart_dir}: could not read Chart.yaml on HEAD; skipping")
            continue

        if head_version != base_version:
            logger.info(f"ok: {chart_dir} version changed {base_version} -> {head_version}")
            continue

        checked += 1
        in_prod = base_version in prod_pins.get(chart_type, set())
        if in_prod:
            new_version = next_patch_version(base_version)
            summary = (
                f"Chart content changed without bumping version {base_version}, "
                f"which is pinned on prod (chart type {chart_type!r})."
            )
            finding = VersionGateFinding(
                check="prod-immutability",
                title=f"Bump `{chart_dir}` before merging",
                file=f"{chart_dir}/Chart.yaml",
                summary=summary,
                fix_steps=[
                    f"Patch-bump the chart version ({base_version} → {new_version}): "
                    f"`uv run --project .github/scripts .github/scripts/rollout/bump_chart_version.py "
                    f"--chart {chart_type} --patch`",
                    f"Commit `{chart_dir}/Chart.yaml` together with your chart edits.",
                    "After merge, wait for the publish workflow to push the new OCI tag, "
                    "then roll it out via `create_update_prs.py` (stage before prod).",
                    f"Alternative: if `{base_version}` is not yet pinned on any prod "
                    f"`helm/*/prod/**/config.yaml`, you may modify the chart in-place "
                    f"without a bump (pre-prod / in-flight publish only).",
                ],
            )
            finding.emit_annotation()
            findings.append(finding)
            failed += 1
        else:
            logger.info(
                f"ok: {chart_dir} modified at {base_version} without bump — "
                f"version not pinned on prod; in-place overwrite allowed"
            )

    logger.info(
        f"validated {checked} alchemy chart(s) modified without version bump; "
        f"{failed} blocked (prod-pinned)"
    )
    if failed:
        print(
            f"::error::found {failed} alchemy chart(s) modified without a version "
            f"bump while pinned on prod — see annotations above"
        )
    return (1 if failed else 0), findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-ref",
        default="origin/master",
        help="Base ref to compare against (default: origin/master).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root (default: current working directory).",
    )
    parser.add_argument(
        "--findings-file",
        type=Path,
        default=None,
        help="Append structured findings as JSONL for the PR comment step.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()

    try:
        code, findings = run_check(args.base_ref, repo_root)
    except GitError as exc:
        finding = VersionGateFinding(
            check="prod-immutability",
            title="Version gate setup failed",
            summary=str(exc),
            fix_steps=[
                "Ensure the checkout has full git history (`fetch-depth: 0`).",
                "Re-run the workflow; if the error persists, check git availability in CI.",
            ],
        )
        finding.emit_annotation()
        append_findings(args.findings_file, [finding])
        return 1
    except Exception as exc:
        logger.exception("prod-immutability gate crashed unexpectedly")
        finding = gate_crash_finding("prod-immutability", exc)
        finding.emit_annotation()
        append_findings(args.findings_file, [finding])
        return 1

    append_findings(args.findings_file, findings)
    return code


if __name__ == "__main__":
    sys.exit(main())
