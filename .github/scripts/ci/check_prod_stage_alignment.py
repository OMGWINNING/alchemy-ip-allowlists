#!/usr/bin/env python3
"""Block prod chartVersion bumps that aren't matched by a stage pin.

Background — SEV2 (May 2026): a rollout PR pinned an alchemy-observability-*
chart to a version that had never been deployed to stage, so the destructive
change in that version (retentionSize) shipped fleet-wide without any prior
real-world exposure.

This check inspects each `helm/*/prod/**/config.yaml` whose `chartVersion:`
line changed in the PR, and refuses to merge unless the same chartVersion is
also pinned on at least one `helm/*/stage/**/config.yaml` of the same chart
type *in HEAD*.

"In HEAD" means either:
- the matching stage config was bumped to the same version in this PR
  (combined stage+prod update), OR
- stage was already at that version on the base ref (sequential rollout
  after the stage-only PR merged separately).

Either way, by the time prod can pin chartVersion X, stage must have already
reached at least X: an exact pin on some `helm/*/stage/**/config.yaml` in HEAD,
or a higher pin when stage has moved ahead during rollout (prod may onboard at
an older version while stage is already on a newer one). Prod pins ahead of
every stage pin are still blocked — that is the SEV2 shape this gate exists to
catch.

Exemptions:
- non-concrete chartVersion pins on the prod side — anything that isn't a
  literal `X.Y.Z` (so `>=…`, `>…`, `^…`, `~…`, `X.Y.x`, `X.Y.*`, `*`, plus
  any pre-release / build-metadata tags) is treated as floating and
  skipped. The gate only enforces exact-version alignment.
- chart types with no concrete stage pin in HEAD (e.g.
  helm/customer-dashboards/ currently has no stage subtree at all). Emit a
  `::warning::` rather than failing — the gate is meaningless without a
  reference set.

Floating stage pins caveat: the reference set only contains concrete
`X.Y.Z` pins; floating stage values (`>=X`, `^X`, `~X`, `X.Y.x`, etc.) are
filtered out because they don't tell us which specific version stage has
actually deployed. Consequence: on a chart type whose stage configs are
*all* floating, the gate becomes unsatisfiable in *any* PR shape —
combined PRs don't help, because the stage pin in HEAD is also floating
and still filtered. To keep the gate enforceable on such a chart type,
keep at least one stage config pinned to a concrete version. If
`helm/*/stage/**` is migrating to floating pins under the platform-rollout
skill's guidance, plan that migration alongside a relaxation of this gate.

Sequential rollouts and rebase discipline: the "stage already at X" path
relies on `helm/*/stage/**` *in HEAD* containing X. HEAD = base ref + the
PR's commits. So when a stage-only PR merges separately and a follow-up
prod-only PR sits on stale base, the prod PR must be rebased (or "Update
branch" used) onto the merged stage before CI re-runs — otherwise the
gate evaluates against a HEAD whose stage tree doesn't yet contain X and
fails closed.

Usage:
    uv run --project .github/scripts python \\
        .github/scripts/ci/check_prod_stage_alignment.py --base-ref origin/master
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.version_gate_findings import VersionGateFinding, append_findings, gate_crash_finding


def chart_type_for_config(config_path: Path) -> str | None:
    """Classify a config.yaml into a chart-type bucket.

    Mirrors check_rollout_chart_versions.py so a future move to a shared
    helper module stays trivial.
    """
    parts = config_path.parts
    if "observability-platform" in parts:
        return "shard"
    if "customer-dashboards" in parts:
        return "grafana"
    if "remote-clusters" in parts:
        # helm/remote-clusters/<env>/<region>/<provider>/<name>/{core|<agent-name>}/config.yaml
        return "core" if config_path.parent.name == "core" else "agent"
    return None


def _env_segment(config_path: Path) -> str | None:
    parts = config_path.parts
    try:
        helm_idx = parts.index("helm")
    except ValueError:
        return None
    if len(parts) <= helm_idx + 2:
        return None
    return parts[helm_idx + 2]


def is_prod_path(config_path: Path) -> bool:
    return _env_segment(config_path) == "prod"


def is_stage_path(config_path: Path) -> bool:
    return _env_segment(config_path) == "stage"


# Strict `MAJOR.MINOR.PATCH` (digits only). Anything else — `>=X`, `>X`,
# `^X`, `~X`, `X.Y.x`, `X.Y.*`, `*`, pre-release tags like `0.0.18-rc1`,
# build metadata — is treated as non-concrete and skipped on both the
# stage-reference-set and prod-side checks. Matches the conservative shape
# of check_rollout_chart_versions.py's parse_semver, which also only
# accepts `X.Y.Z`.
_CONCRETE_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def is_concrete_pin(v: str) -> bool:
    """True iff v is a literal `X.Y.Z` we can use for exact-equality matching."""
    return bool(_CONCRETE_SEMVER_RE.match(v))


def parse_semver(v: str) -> tuple[int, int, int] | None:
    try:
        major, minor, patch = (int(p) for p in v.split("."))
    except ValueError:
        return None
    return major, minor, patch


def prod_pin_satisfied_by_stage(pinned: str, stage_pins: set[str]) -> bool:
    """True when stage has already reached `pinned` (exact or ahead)."""
    if pinned in stage_pins:
        return True
    pinned_semver = parse_semver(pinned)
    if pinned_semver is None:
        return False
    stage_semvers = [s for v in stage_pins if (s := parse_semver(v)) is not None]
    if not stage_semvers:
        return False
    return pinned_semver <= max(stage_semvers)


def read_chart_version(config_path: Path) -> str | None:
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
    except FileNotFoundError:
        return None
    except yaml.YAMLError as exc:
        print(f"::warning file={config_path}::malformed YAML ({exc}); skipping")
        return None
    v = data.get("chartVersion")
    return None if v is None else str(v).strip()


class GitDiffError(RuntimeError):
    """Raised when a git invocation the gate depends on fails.

    Caught by main(), which exits non-zero. The gate must fail closed on
    broken setup — a silent pass would defeat the SEV2 the gate exists to
    prevent.
    """


def chartversion_line_changed(file: str, base_ref: str, repo_root: Path) -> bool:
    """True iff the `chartVersion:` line itself was added/removed in the diff.

    Raises GitDiffError on any non-zero rc from `git diff`. Earlier this
    function silently swallowed errors as "no change detected", which
    contradicted the fail-closed posture established for git_changed_files:
    a transient git error here would let a prod chartVersion bump slip
    through the gate. The base ref is already validated by git_changed_files
    upstream, so any failure here is genuine.
    """
    result = subprocess.run(
        ["git", "diff", "--unified=0", f"{base_ref}...HEAD", "--", file],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise GitDiffError(
            f"per-file `git diff` failed for {file} against {base_ref} "
            f"(rc={result.returncode}): {result.stderr.strip()}"
        )
    return bool(re.search(r"^[+-]\s*chartVersion:", result.stdout, re.MULTILINE))


def git_changed_files(base_ref: str, repo_root: Path) -> list[Path]:
    """Files added/modified/renamed between base_ref and HEAD.

    Raises GitDiffError when git itself can't produce a list — that is a
    broken-setup signal, not "no files changed", and the caller must fail
    closed rather than treating it as a pass.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise GitDiffError(
            f"git diff against {base_ref} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return [Path(line) for line in result.stdout.splitlines() if line]


def collect_stage_versions(repo_root: Path) -> dict[str, set[str]]:
    """Walk HEAD's working tree for every stage config.yaml.

    Returns chart_type -> set of pinned chartVersion strings. Only concrete
    `X.Y.Z` pins are included; floating ranges (`>=…`, `^…`, `~…`, etc.) are
    excluded — they don't identify a specific deployed version and can't be
    matched by equality.
    """
    stage_versions: dict[str, set[str]] = {}
    for config in (repo_root / "helm").rglob("config.yaml"):
        rel = config.relative_to(repo_root)
        if not is_stage_path(rel):
            continue
        chart_type = chart_type_for_config(rel)
        if chart_type is None:
            continue
        v = read_chart_version(config)
        if v is None or not is_concrete_pin(v):
            continue
        stage_versions.setdefault(chart_type, set()).add(v)
    return stage_versions


def run_check(base_ref: str, repo_root: Path) -> tuple[int, list[VersionGateFinding]]:
    findings: list[VersionGateFinding] = []
    changed = git_changed_files(base_ref, repo_root)

    if not changed:
        logger.info("No changed files vs base ref; nothing to validate.")
        return 0, findings

    prod_candidates = [
        p
        for p in changed
        if p.name == "config.yaml" and is_prod_path(p) and chart_type_for_config(p)
    ]
    if not prod_candidates:
        logger.info("No prod config.yaml files changed; skipping stage-alignment check.")
        return 0, findings

    stage_versions = collect_stage_versions(repo_root)
    if stage_versions:
        for ct, vs in sorted(stage_versions.items()):
            logger.info(f"stage pins for chart_type={ct!r}: {sorted(vs)}")
    else:
        logger.info("No concrete stage chartVersion pins found in HEAD.")

    failed = 0
    checked = 0
    for rel_path in prod_candidates:
        chart_type = chart_type_for_config(rel_path)
        assert chart_type is not None

        if not chartversion_line_changed(str(rel_path), base_ref, repo_root):
            continue

        head_path = repo_root / rel_path
        pinned = read_chart_version(head_path)
        if pinned is None:
            continue
        if not is_concrete_pin(pinned):
            logger.info(f"skip non-concrete prod chartVersion {pinned!r}: {rel_path}")
            continue

        stage_pins = stage_versions.get(chart_type, set())

        if not stage_pins:
            print(
                f"::warning file={rel_path}::no stage config of chart type "
                f"{chart_type!r} found in HEAD; cannot enforce stage-alignment "
                f"for this file"
            )
            continue

        checked += 1
        if prod_pin_satisfied_by_stage(pinned, stage_pins):
            if pinned in stage_pins:
                logger.info(f"ok: {rel_path} pins {chart_type}@{pinned} (matched on stage)")
            else:
                max_stage = max(stage_pins, key=lambda v: parse_semver(v) or (0, 0, 0))
                logger.info(
                    f"ok: {rel_path} pins {chart_type}@{pinned} "
                    f"(stage has moved ahead to {max_stage})"
                )
            continue

        observed = ", ".join(sorted(stage_pins))
        summary = (
            f"Prod pins chartVersion {pinned} for chart type {chart_type!r}, but no "
            f"helm/*/stage/**/config.yaml of the same chart type has reached {pinned} "
            f"in HEAD. Versions currently pinned on stage: {observed}."
        )
        finding = VersionGateFinding(
            check="stage-alignment",
            title=f"Prod pin `{pinned}` not reached on stage yet",
            file=str(rel_path),
            summary=summary,
            fix_steps=[
                f"**Option A — combined PR:** in this same PR, bump at least one matching "
                f"`helm/*/stage/**/config.yaml` for chart type `{chart_type}` to at least "
                f"`chartVersion: {pinned}`.",
                f"**Option B — sequential rollout:** merge the stage rollout for `{pinned}` "
                f"first (via `create_update_prs.py`), then rebase this prod PR onto master "
                f"(`git fetch origin && git rebase origin/master` or GitHub **Update branch**) "
                f"so HEAD's stage tree has also reached `{pinned}`.",
                "There is no override label — fix the rollout shape, not the gate.",
                "This gate exists because the May 2026 SEV2 shipped a prod pin that had "
                "never been exposed on stage.",
            ],
        )
        finding.emit_annotation()
        findings.append(finding)
        failed += 1

    logger.info(f"validated {checked} prod chartVersion pin(s); {failed} unaligned")
    if failed:
        print(
            f"::error::found {failed} prod chartVersion pin(s) not aligned "
            f"with stage — see annotations above"
        )
    return (1 if failed else 0), findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        # The docstring is multi-paragraph and hand-wrapped for readability;
        # the default formatter would re-wrap it on `--help`.
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
    except GitDiffError as exc:
        finding = VersionGateFinding(
            check="stage-alignment",
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
        logger.exception("stage-alignment gate crashed unexpectedly")
        finding = gate_crash_finding("stage-alignment", exc)
        finding.emit_annotation()
        append_findings(args.findings_file, [finding])
        return 1

    append_findings(args.findings_file, findings)
    return code


if __name__ == "__main__":
    sys.exit(main())
