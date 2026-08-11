#!/usr/bin/env python3
"""Assert producer sampleAgeLimit stays below the shard's OOO ingestion window.

Background — SEV1 (June 5, 2026), "Observability down in APSE": a Prometheus
shard restart (s-ep-ovh-c-prd-apse1) produced a WAL backlog (~55m) older than
the receiver's out-of-order (OOO) ingestion window (30m at the time). The
receiver rejected every remote-write batch with HTTP 400 "too old sample", the
shard retried continuously, exhausted its retry budget, and permanently
dropped all samples from the outage window.

The fix pairs two guardrails that must agree in relative size:
  - producer-side `queueConfig.sampleAgeLimit` on remote Prometheus (core/
    agent) — silently drop samples older than this before they are ever sent,
    so a slow/rejecting receiver cannot trigger a retry storm.
  - consumer-side `tsdb.outOfOrderTimeWindow` on the shard — how far back the
    receiver will accept an out-of-order sample.

sampleAgeLimit must stay strictly below outOfOrderTimeWindow: a sample the
producer would still try to send must land inside the window the shard is
willing to accept. Both are hand-maintained YAML in separate charts, so
nothing but convention kept them aligned before this gate.

Producer sources: alchemy-observability-core/patches/*.yaml and
alchemy-observability-agent/patches/*.yaml — remoteWrite entries whose url
targets a shard replica endpoint (`.../write/replica-N`).

Consumer source: alchemy-observability-shard/values.yaml —
kube-prometheus-stack.prometheus.prometheusSpec.tsdb.outOfOrderTimeWindow.

Usage:
    uv run --project .github/scripts python \\
        .github/scripts/ci/check_queue_window_alignment.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.version_gate_findings import VersionGateFinding, append_findings, gate_crash_finding

CHECK_NAME = "queue-window-alignment"

# Only remote-write entries targeting a shard replica are governed by this
# invariant — e.g. the Thanos long-term-storage entry has its own queueConfig
# but no sampleAgeLimit and a different (unrelated) receiver.
_SHARD_REPLICA_URL_RE = re.compile(r"/write/replica-\d+")

_DURATION_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}
# Prometheus model.ParseDuration shape: one or more `<number><unit>` pairs,
# e.g. "89m", "1h30m". No whitespace between pairs.
_DURATION_RE = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")


def parse_duration(value: str) -> float:
    """Parse a Prometheus-style duration string into seconds."""
    matches = _DURATION_RE.findall(value)
    if not matches or "".join(f"{n}{u}" for n, u in matches) != value:
        raise ValueError(f"not a valid Prometheus duration: {value!r}")
    return sum(int(n) * _DURATION_UNIT_SECONDS[u] for n, u in matches)


def _iter_dicts(node: Any):
    """Recursively yield every dict found anywhere inside node."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _load_yaml_docs(path: Path) -> list[Any]:
    """Load a file as one or more YAML documents.

    Patch files are sometimes a bare values mapping and sometimes a ConfigMap
    wrapper whose `data.values.yaml` key holds an embedded YAML string (e.g.
    alchemy-observability-core/patches/remotewrite-full.yaml). Both shapes are
    returned as top-level docs to search uniformly.
    """
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
    embedded = []
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            raw = doc.get("data", {}).get("values.yaml")
            if isinstance(raw, str):
                embedded.extend(d for d in yaml.safe_load_all(raw) if d is not None)
    return docs + embedded


def find_producer_sample_age_limits(patch_dir: Path) -> list[tuple[str, str, float]]:
    """Return (file, url, sample_age_limit_seconds) for every shard-replica remoteWrite entry."""
    found: list[tuple[str, str, float]] = []
    for path in sorted(patch_dir.glob("*.yaml")):
        for doc in _load_yaml_docs(path):
            for d in _iter_dicts(doc):
                url = d.get("url")
                queue_config = d.get("queueConfig")
                if not isinstance(url, str) or not isinstance(queue_config, dict):
                    continue
                if not _SHARD_REPLICA_URL_RE.search(url):
                    continue
                limit = queue_config.get("sampleAgeLimit")
                if limit is None:
                    continue
                found.append((str(path), url, parse_duration(str(limit))))
    return found


def find_consumer_ooo_window(shard_values_path: Path) -> float:
    data = yaml.safe_load(shard_values_path.read_text()) or {}
    try:
        raw = data["kube-prometheus-stack"]["prometheus"]["prometheusSpec"]["tsdb"][
            "outOfOrderTimeWindow"
        ]
    except (KeyError, TypeError) as exc:
        raise KeyError(
            f"kube-prometheus-stack.prometheus.prometheusSpec.tsdb.outOfOrderTimeWindow "
            f"not found in {shard_values_path}"
        ) from exc
    return parse_duration(str(raw))


def run_check(repo_root: Path) -> tuple[int, list[VersionGateFinding]]:
    findings: list[VersionGateFinding] = []

    shard_values_path = repo_root / "alchemy-observability-shard" / "values.yaml"
    ooo_window = find_consumer_ooo_window(shard_values_path)
    logger.info(f"consumer outOfOrderTimeWindow={ooo_window:g}s ({shard_values_path})")

    producer_dirs = [
        repo_root / "alchemy-observability-core" / "patches",
        repo_root / "alchemy-observability-agent" / "patches",
    ]

    checked = 0
    failed = 0
    for patch_dir in producer_dirs:
        if not patch_dir.is_dir():
            continue
        for file, url, limit in find_producer_sample_age_limits(patch_dir):
            checked += 1
            rel_file = Path(file).relative_to(repo_root)
            if limit < ooo_window:
                logger.info(
                    f"ok: {rel_file} sampleAgeLimit={limit:g}s < ooo={ooo_window:g}s ({url})"
                )
                continue

            failed += 1
            summary = (
                f"{rel_file} sets queueConfig.sampleAgeLimit={limit:g}s for {url!r}, "
                f"which is not less than the shard's outOfOrderTimeWindow="
                f"{ooo_window:g}s ({shard_values_path.relative_to(repo_root)}). A backlog the "
                f"producer still tries to send would fall outside the receiver's OOO window "
                f"and be rejected — the same retry-storm failure mode as the June 2026 SEV1."
            )
            finding = VersionGateFinding(
                check=CHECK_NAME,
                title="Producer sampleAgeLimit not below shard outOfOrderTimeWindow",
                file=str(rel_file),
                summary=summary,
                fix_steps=[
                    f"Lower `sampleAgeLimit` in {rel_file} below {ooo_window:g}s, or",
                    f"raise `outOfOrderTimeWindow` in "
                    f"{shard_values_path.relative_to(repo_root)} above {limit:g}s, "
                    f"whichever matches the intended guardrail.",
                    "Keep a comfortable margin (repo convention: 1m) rather than an exact match.",
                ],
            )
            finding.emit_annotation()
            findings.append(finding)

    logger.info(f"validated {checked} producer sampleAgeLimit setting(s); {failed} misaligned")
    if failed:
        print(f"::error::found {failed} producer/consumer queue-window misalignment(s)")
    return (1 if failed else 0), findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        code, findings = run_check(repo_root)
    except Exception as exc:
        logger.exception(f"{CHECK_NAME} gate crashed unexpectedly")
        finding = gate_crash_finding(CHECK_NAME, exc)
        finding.emit_annotation()
        append_findings(args.findings_file, [finding])
        return 1

    append_findings(args.findings_file, findings)
    return code


if __name__ == "__main__":
    sys.exit(main())
