"""Helm Quality Checks worker — render gate.

This is the Python port of the heavy bash blocks that used to live inline in
`.github/workflows/helm-quality-checks.yaml`. One subcommand:

  render-gate   Render every chart root (helm dependency build + helm template)
                across a process pool and run the declarative rendered-manifest
                guards from rendered-manifest-guards.yaml. Exit non-zero on any
                failure — this is the gate.

The PR-comment preview/diff previously lived here too; it now runs from the
shared `helm-diff` CLI in cloud-infra-tools (see the `preview` job in
`.github/workflows/helm-quality-checks.yaml`).

Invoked via `uv run --project .github/scripts python .github/scripts/ci/helm_quality.py <cmd>`,
matching the repo convention for the other check_*.py scripts. Pure stdlib +
pyyaml; helm/grep are shelled out so guard-pattern semantics stay byte-identical
to the previous bash.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def find_chart_roots(repo: Path) -> list[Path]:
    """Every Chart.yaml the render gate visits.

    Mirrors the workflow's `find . -type f -name Chart.yaml -not -path
    './.git/*' -not -path '*/charts/*' -not -path '*/patches/*' -not -path
    './.github/*'`: skip vendored subchart dirs (charts/), placeholder template
    charts (patches/, which contain ${CHART_VERSION} and would fail semver), and
    the .git/.github trees.
    """
    roots: list[Path] = []
    for chartfile in repo.rglob("Chart.yaml"):
        rel = chartfile.relative_to(repo)
        dirs = rel.parts[:-1]
        if rel.parts and rel.parts[0] in (".git", ".github"):
            continue
        if "charts" in dirs or "patches" in dirs:
            continue
        roots.append(chartfile)
    return sorted(roots)


def _chart_metadata(chartfile: Path) -> dict:
    """Return parsed Chart.yaml metadata, or an empty mapping on failure."""
    try:
        return yaml.safe_load(chartfile.read_text()) or {}
    except yaml.YAMLError, OSError:
        return {}


def _chart_identity(chartfile: Path) -> tuple[str, list[dict[str, str]]]:
    """Return (Chart.yaml `name`, dependency metadata) for guard matching."""
    data = _chart_metadata(chartfile)
    name = str(data.get("name", "") or "")
    deps = data.get("dependencies") or []
    dep_meta = [
        {"name": str(d["name"]), "version": str(d.get("version", "") or "")}
        for d in deps
        if isinstance(d, dict) and d.get("name")
    ]
    return name, dep_meta


def _chart_allows_empty_render(chartfile: Path, values_file: Path | None) -> bool:
    """Whether one explicitly named inactive values variant may render empty."""
    if values_file is None:
        return False
    annotations = _chart_metadata(chartfile).get("annotations") or {}
    raw_variants = annotations.get("observability.alchemy.com/allow-empty-render-values", "")
    allowed_variants = {name.strip() for name in raw_variants.split(",") if name.strip()}
    return values_file.name in allowed_variants


def _grep_hits(text: str, pattern: str) -> list[str]:
    """Replicate `grep -v '^[[:space:]]*#' | grep -E -- "$pattern"`.

    Shelling out to grep (rather than Python `re`) keeps POSIX ERE semantics —
    including `[[:space:]]` classes and `--`-prefixed patterns like
    `--storage.tsdb.retention.size` — identical to the previous workflow, so a
    SEV2 guard can't silently weaken on the port.
    """
    p1 = _run(["grep", "-v", "^[[:space:]]*#"], input=text)
    stripped = p1.stdout
    if not stripped:
        return []
    p2 = _run(["grep", "-E", "--", pattern], input=stripped)
    return [ln for ln in p2.stdout.splitlines() if ln]


def guard_applies(guard: dict, chart_name: str, dep_meta: list[dict[str, str]]) -> bool:
    """A guard applies if its match_chart_name equals the chart name, or its
    match_chart_dependency appears in the chart's dependencies (the latter
    catches per-instance charts under helm/ that pull a guarded chart in).

    match_chart_dependency_version optionally narrows dependency matching to
    one exact dependency version, which keeps version-specific required-pattern
    guards from firing against older rollout pins that cannot render the new
    contract yet.
    """
    match_name = guard.get("match_chart_name", "")
    match_dep = guard.get("match_chart_dependency", "")
    match_dep_version = guard.get("match_chart_dependency_version", "")
    if match_name and chart_name == match_name:
        return True
    if not match_dep:
        return False
    for dep in dep_meta:
        if dep["name"] != match_dep:
            continue
        if match_dep_version and dep["version"] != match_dep_version:
            continue
        return True
    return False


# --------------------------------------------------------------------------- #
# render-gate
# --------------------------------------------------------------------------- #
def _check_guards(
    chartfile: Path, out: str, label: str, guards: list[dict]
) -> tuple[list[str], bool]:
    logs: list[str] = []
    ok = True
    if not out:
        return logs, ok
    chart_name, dep_meta = _chart_identity(chartfile)
    for guard in guards:
        if not guard_applies(guard, chart_name, dep_meta):
            continue
        gid = guard["id"]
        desc = guard["description"]
        remediation = guard.get("remediation", "")
        for pat in guard.get("forbidden_patterns", []):
            hits = _grep_hits(out, pat)
            if hits:
                fix = f" Fix: {remediation}" if remediation else ""
                logs.append(
                    f"::error file={chartfile}::[guard:{gid}] {label} matched "
                    f"forbidden pattern /{pat}/. {desc}{fix}"
                )
                logs.extend(f"  {ln}" for ln in hits[:10])
                ok = False
        for pat in guard.get("required_patterns", []):
            hits = _grep_hits(out, pat)
            if not hits:
                fix = f" Fix: {remediation}" if remediation else ""
                logs.append(
                    f"::error file={chartfile}::[guard:{gid}] {label} did not match "
                    f"required pattern /{pat}/. {desc}{fix}"
                )
                ok = False
    return logs, ok


def _process_chart(payload: dict) -> dict:
    """Worker: dependency-build (if needed) + template every variant of one
    chart, assert it renders >=1 object, and run guards. Returns a result dict
    (must be picklable for the process pool); the parent prints logs in a
    deterministic order so pooled output doesn't interleave."""
    chartfile = Path(payload["chartfile"])
    guards: list[dict] = payload["guards"]
    chart = chartfile.parent
    logs: list[str] = []
    ok = True

    # Build deps only when charts/ is missing — actions/cache may have restored
    # it on a Chart.yaml-hash hit. On miss, let `helm dependency build` run its
    # implicit `helm repo update` (a fresh runner has no cached index).
    if not (chart / "charts").is_dir():
        r = _run(["helm", "dependency", "build", str(chart)])
        if r.returncode != 0:
            logs.append(f"::error file={chartfile}::helm dependency build failed for {chart}")
            logs.append((r.stdout + r.stderr).rstrip())
            return {"chart": str(chart), "ok": False, "logs": logs}

    # Top-level wrappers keep cluster knobs in values-<cluster>.yaml; render once
    # per variant. helm/ charts have a single generated values.yaml → render with
    # defaults.
    variants = sorted(chart.glob("values-*.yaml"))
    for vf in variants or [None]:
        if vf is not None:
            label = f"{chart} (with {vf.name})"
            r = _run(["helm", "template", "r", str(chart), "-f", str(vf)])
            errfile = str(vf)
        else:
            label = str(chart)
            r = _run(["helm", "template", "r", str(chart)])
            errfile = str(chartfile)
        if r.returncode != 0:
            logs.append(f"::error file={errfile}::helm template failed for {label}")
            logs.append((r.stdout + r.stderr).rstrip())
            ok = False
            continue
        out = r.stdout
        kinds = sum(1 for ln in out.splitlines() if ln.startswith("kind:"))
        if kinds < 1:
            if _chart_allows_empty_render(chartfile, vf):
                logs.append(f"::notice file={chartfile}::{label} intentionally rendered 0 objects")
            else:
                logs.append(f"::error file={chartfile}::{label} rendered 0 objects")
                ok = False
            continue
        guard_logs, guard_ok = _check_guards(chartfile, out, label, guards)
        logs.extend(guard_logs)
        ok = ok and guard_ok

    return {"chart": str(chart), "ok": ok, "logs": logs}


def cmd_render_gate(args) -> int:
    repo = Path(args.repo).resolve()
    guards = yaml.safe_load(Path(args.guards).read_text())["guards"]
    charts = find_chart_roots(repo)
    payloads = [{"chartfile": str(c), "guards": guards} for c in charts]

    workers = args.jobs or os.cpu_count() or 4
    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_process_chart, payloads))

    failed = False
    for res in sorted(results, key=lambda r: r["chart"]):
        for line in res["logs"]:
            print(line)
        if not res["ok"]:
            failed = True

    print(
        f"Rendered {len(charts)} chart roots across {workers} workers: "
        f"{'FAILED' if failed else 'OK'}"
    )
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    rg = sub.add_parser("render-gate", help="render every chart + run guards")
    rg.add_argument("--repo", default=".")
    rg.add_argument("--guards", default=".github/rendered-manifest-guards.yaml")
    rg.add_argument("--jobs", type=int, default=0, help="0 = os.cpu_count()")
    rg.set_defaults(func=cmd_render_gate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
