"""Shared findings + sticky PR comment helpers for version-gate CI checks."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

COMMENT_MARKER = "<!-- observability-infra-charts:version-gates -->"


@dataclass
class VersionGateFinding:
    """One actionable version-gate failure."""

    check: str
    title: str
    summary: str
    fix_steps: list[str] = field(default_factory=list)
    file: str | None = None

    def emit_annotation(self) -> None:
        """Print a GitHub Actions workflow annotation."""
        location = f" file={self.file}" if self.file else ""
        print(f"::error{location}::{self.check}: {self.summary}")


def gate_crash_finding(check: str, exc: Exception) -> VersionGateFinding:
    """Finding for an unexpected gate crash so the PR comment is not silently cleared."""
    return VersionGateFinding(
        check=check,
        title="Version gate crashed unexpectedly",
        summary=f"{type(exc).__name__}: {exc}",
        fix_steps=[
            "Check the workflow log for the full stack trace.",
            "Re-run CI; if it persists, this is likely a bug in the version-gate script.",
        ],
    )


def append_findings(path: Path | None, findings: list[VersionGateFinding]) -> None:
    if not path or not findings:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for finding in findings:
            fh.write(json.dumps(asdict(finding), ensure_ascii=False) + "\n")


def read_findings(path: Path | None) -> list[VersionGateFinding]:
    if path is None or not path.is_file():
        return []
    findings: list[VersionGateFinding] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        findings.append(VersionGateFinding(**data))
    return findings


def format_markdown(findings: list[VersionGateFinding]) -> str:
    """Render findings as PR-comment markdown."""
    if not findings:
        return ""

    sections: list[str] = []
    for i, finding in enumerate(findings, 1):
        loc = f" (`{finding.file}`)" if finding.file else ""
        block = [f"### {i}. {finding.title}{loc}", "", finding.summary, ""]
        if finding.fix_steps:
            block.append("**How to fix:**")
            block.extend(f"{j}. {step}" for j, step in enumerate(finding.fix_steps, 1))
        sections.append("\n".join(block))

    return (
        "One or more **Rollout chart-version gates** failed. "
        "Fix every item below, push, and CI will re-run.\n\n"
        + "\n\n---\n\n".join(sections)
        + "\n\n---\n\n"
        + "_This comment updates automatically on each push. "
        + "It is removed when all version gates pass._"
    )


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)


def _comment_index(repo: str, pr: str) -> dict[str, int]:
    idx: dict[str, int] = {}
    page = 1
    while True:
        r = _run(["gh", "api", f"repos/{repo}/issues/{pr}/comments?per_page=100&page={page}"])
        if r.returncode != 0:
            raise RuntimeError(f"gh api list comments failed: {r.stderr.strip()}")
        comments: list[dict[str, Any]] = json.loads(r.stdout)
        for comment in comments:
            if (comment.get("user") or {}).get("login") != "github-actions[bot]":
                continue
            first = (comment.get("body") or "").split("\n", 1)[0]
            idx.setdefault(first, comment["id"])
        if len(comments) < 100:
            break
        page += 1
    return idx


def _post_body(args: list[str], body: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    try:
        r = _run([*args, "--field", f"body=@{path}"])
        if r.returncode != 0:
            raise RuntimeError(f"gh api failed ({args}): {r.stderr.strip()}")
    finally:
        os.unlink(path)


def upsert_version_gate_comment(repo: str, pr: str, body: str) -> None:
    """Create or update the sticky version-gate comment."""
    idx = _comment_index(repo, pr)
    header = f"{COMMENT_MARKER}\n## Version gate failures — how to fix\n\n"
    full_body = header + body
    cid = idx.get(COMMENT_MARKER)
    if cid:
        _post_body(["gh", "api", "-X", "PATCH", f"repos/{repo}/issues/comments/{cid}"], full_body)
    else:
        _post_body(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{pr}/comments"], full_body)


def delete_version_gate_comment(repo: str, pr: str) -> None:
    """Remove the sticky comment when all gates pass."""
    idx = _comment_index(repo, pr)
    cid = idx.get(COMMENT_MARKER)
    if not cid:
        return
    result = _run(["gh", "api", "-X", "DELETE", f"repos/{repo}/issues/comments/{cid}"])
    if result.returncode != 0:
        print(
            f"::warning::could not delete version-gate PR comment (id={cid}): "
            f"{result.stderr.strip()}"
        )
