#!/usr/bin/env python3
"""Upsert or remove the sticky version-gate PR comment from collected findings."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.version_gate_findings import (
    delete_version_gate_comment,
    format_markdown,
    read_findings,
    upsert_version_gate_comment,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--findings-file",
        type=Path,
        required=True,
        help="JSONL file appended by version-gate check scripts.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub repository as owner/name (default: $GITHUB_REPOSITORY).",
    )
    parser.add_argument(
        "--pr",
        default=os.environ.get("PR_NUMBER", ""),
        help="Pull request number (default: $PR_NUMBER).",
    )
    args = parser.parse_args()

    if not args.repo or not args.pr:
        logger.info("No repo/pr context; skipping PR comment (local run).")
        return 0

    findings = read_findings(args.findings_file)
    if not findings:
        logger.info("No version-gate findings; removing stale PR comment if present.")
        try:
            delete_version_gate_comment(args.repo, args.pr)
        except RuntimeError as exc:
            print(f"::warning::could not delete version-gate PR comment: {exc}")
        return 0

    body = format_markdown(findings)
    try:
        upsert_version_gate_comment(args.repo, args.pr, body)
    except RuntimeError as exc:
        print(f"::warning::could not post version-gate PR comment: {exc}")
        return 0

    logger.info(f"Posted version-gate PR comment with {len(findings)} finding(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
