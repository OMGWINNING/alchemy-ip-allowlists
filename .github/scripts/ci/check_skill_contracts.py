#!/usr/bin/env python3
"""Validate repository-local observability skill contracts."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ALLOWED_APPROVALS = {"not-required", "required-before-mutation"}
ALLOWED_MUTATION_SCOPES = {
    "aws-secrets-manager",
    "github",
    "grafana",
    "kubernetes",
    "repository",
}
EXTERNAL_SKILL_REFERENCES = {
    "alchemy-observability-grafana",
    "loki-log-explorer",
    "observability-platform-admin",
    "observability-platform-developer",
}
# Descriptions and Cross-skill references are contract surfaces: every
# backtick-wrapped lowercase hyphenated token there is treated as a skill name.
# Keep non-skill terms out of backticks in those regions, or allowlist an
# intentional exception above, so misspelled and retired skill names fail closed.
INLINE_CODE_RE = re.compile(r"`([a-z][a-z0-9]+(?:-[a-z0-9]+)+)`")
MCP_TOOL_RE = re.compile(r"\bmcp__[A-Za-z0-9_-]+")
APPROVAL_HEADING_RE = re.compile(r"^## Approval Gate\s*$", re.MULTILINE)
CROSS_SKILL_HEADING_RE = re.compile(r"^## Cross-skill references\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SkillContract:
    """Machine-readable safety contract loaded from a skill frontmatter block."""

    name: str
    description: str
    mutation_scopes: tuple[str, ...]
    approval: str
    path: Path


def _display_path(path: Path, repo_root: Path) -> str:
    """Return a stable repository-relative path when possible."""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _frontmatter(skill_file: Path) -> dict[str, Any]:
    """Parse and return a skill's YAML frontmatter."""
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing frontmatter")

    try:
        closing_index = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("missing frontmatter closing delimiter") from exc

    try:
        data = yaml.safe_load("\n".join(lines[1:closing_index]))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid frontmatter YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    return data


def load_contract(skill_file: Path) -> SkillContract:
    """Load and type-check the frontmatter contract for one skill file."""
    data = _frontmatter(skill_file)

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a non-empty string")

    raw_scopes = data.get("mutation_scopes")
    if not isinstance(raw_scopes, list) or not all(isinstance(scope, str) for scope in raw_scopes):
        raise ValueError("mutation_scopes must be a list of strings")

    approval = data.get("approval")
    if not isinstance(approval, str):
        raise ValueError("approval must be a string")

    return SkillContract(
        name=name.strip(),
        description=description.strip(),
        mutation_scopes=tuple(raw_scopes),
        approval=approval,
        path=skill_file,
    )


def discover_skills(skills_root: Path) -> list[Path]:
    """Return direct repository skill files, excluding nested worktree copies."""
    if not skills_root.is_dir():
        return []
    return sorted(skills_root.glob("*/SKILL.md"))


def _cross_skill_text(skill_text: str) -> str:
    """Extract the Cross-skill references section from a skill document."""
    match = CROSS_SKILL_HEADING_RE.search(skill_text)
    if match is None:
        return ""
    remainder = skill_text[match.end() :]
    next_heading = re.search(r"^## ", remainder, re.MULTILINE)
    return remainder[: next_heading.start()] if next_heading else remainder


def _validate_references(
    contracts: list[SkillContract],
    repo_root: Path,
) -> list[str]:
    """Validate local references in skill descriptions, cross-reference tables, and the router."""
    errors: list[str] = []
    local_names = {contract.name for contract in contracts}
    allowed_names = local_names | EXTERNAL_SKILL_REFERENCES

    for contract in contracts:
        skill_text = contract.path.read_text(encoding="utf-8")
        reference_text = f"{contract.description}\n{_cross_skill_text(skill_text)}"
        for reference in sorted(set(INLINE_CODE_RE.findall(reference_text))):
            if reference not in allowed_names:
                path = _display_path(contract.path, repo_root)
                errors.append(f"{path}: dangling local skill reference `{reference}`")

    claude_file = repo_root / "CLAUDE.md"
    if claude_file.is_file():
        claude_text = claude_file.read_text(encoding="utf-8")
        for reference in sorted(set(re.findall(r"→\s*`([^`]+)`", claude_text))):
            if reference not in allowed_names:
                path = _display_path(claude_file, repo_root)
                errors.append(f"{path}: dangling router skill reference `{reference}`")

    return errors


def _validate_mcp_references(skill_files: list[Path], repo_root: Path) -> list[str]:
    """Reject direct references to retired MCP tool names."""
    errors: list[str] = []
    for skill_file in skill_files:
        for line_number, line in enumerate(skill_file.read_text(encoding="utf-8").splitlines(), 1):
            for match in MCP_TOOL_RE.finditer(line):
                path = _display_path(skill_file, repo_root)
                errors.append(
                    f"{path}:{line_number}: direct MCP tool reference `{match.group(0)}` is forbidden"
                )
    return errors


def validate_contracts(repo_root: Path) -> list[str]:
    """Return deterministic, human-readable skill contract violations."""
    repo_root = repo_root.resolve()
    skill_files = discover_skills(repo_root / ".agents" / "skills")
    contracts: list[SkillContract] = []
    errors: list[str] = []

    for skill_file in skill_files:
        path = _display_path(skill_file, repo_root)
        try:
            contract = load_contract(skill_file)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue

        contracts.append(contract)
        directory_name = skill_file.parent.name
        if contract.name != directory_name:
            errors.append(
                f"{path}: skill name `{contract.name}` does not match directory `{directory_name}`"
            )

        unknown_scopes = sorted(set(contract.mutation_scopes) - ALLOWED_MUTATION_SCOPES)
        if unknown_scopes:
            errors.append(f"{path}: unknown mutation scope(s): {', '.join(unknown_scopes)}")

        if contract.approval not in ALLOWED_APPROVALS:
            errors.append(f"{path}: approval must be one of {', '.join(sorted(ALLOWED_APPROVALS))}")
        elif contract.mutation_scopes and contract.approval != "required-before-mutation":
            errors.append(f"{path}: non-empty mutation_scopes require required-before-mutation")
        elif not contract.mutation_scopes and contract.approval != "not-required":
            errors.append(f"{path}: empty mutation_scopes require approval: not-required")

        if contract.mutation_scopes:
            skill_text = skill_file.read_text(encoding="utf-8")
            if APPROVAL_HEADING_RE.search(skill_text) is None:
                errors.append(f"{path}: mutating skill is missing `## Approval Gate`")

    by_name: dict[str, list[SkillContract]] = defaultdict(list)
    for contract in contracts:
        by_name[contract.name].append(contract)
    for name, duplicates in sorted(by_name.items()):
        if len(duplicates) < 2:
            continue
        paths = ", ".join(
            sorted(_display_path(contract.path, repo_root) for contract in duplicates)
        )
        for contract in duplicates:
            path = _display_path(contract.path, repo_root)
            errors.append(f"{path}: duplicate skill name `{name}` also declared by {paths}")

    errors.extend(_validate_references(contracts, repo_root))
    errors.extend(_validate_mcp_references(skill_files, repo_root))
    return sorted(set(errors))


def main() -> int:
    """Run contract validation for a repository root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root (default: current working directory).",
    )
    args = parser.parse_args()

    errors = validate_contracts(args.repo_root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    skill_count = len(discover_skills(args.repo_root / ".agents" / "skills"))
    print(f"Validated {skill_count} skill contract(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
