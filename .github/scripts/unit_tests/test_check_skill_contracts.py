"""Unit tests for the repository skill-contract checker."""

from __future__ import annotations

from pathlib import Path

from ci.check_skill_contracts import validate_contracts  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str = "Test skill.",
    mutation_scopes: tuple[str, ...] = (),
    approval: str = "not-required",
    body: str = "# Test Skill\n",
) -> Path:
    """Write a minimal skill fixture and return its path."""
    skill_dir = root / ".agents" / "skills" / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    scopes = ", ".join(mutation_scopes)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "\n".join(
            [
                "---",
                f"name: {name or directory}",
                f"description: {description}",
                f"mutation_scopes: [{scopes}]",
                f"approval: {approval}",
                "---",
                body,
            ]
        ),
        encoding="utf-8",
    )
    return skill_file


def _messages(root: Path) -> list[str]:
    """Return validation messages for a fixture repository."""
    return validate_contracts(root)


def test_read_only_skill_is_valid(tmp_path: Path) -> None:
    _write_skill(tmp_path, "read-only")

    assert validate_contracts(tmp_path) == []


def test_mutating_skill_requires_approval_gate(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "mutating",
        mutation_scopes=("repository",),
        approval="required-before-mutation",
        body="# Mutating\n\n## Approval Gate\n\nShow the diff and obtain explicit confirmation.\n",
    )

    assert validate_contracts(tmp_path) == []


def test_missing_frontmatter_fails(tmp_path: Path) -> None:
    skill_file = tmp_path / ".agents" / "skills" / "broken" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# No frontmatter\n", encoding="utf-8")

    messages = _messages(tmp_path)

    assert any(
        "broken/SKILL.md" in message and "missing frontmatter" in message for message in messages
    )


def test_directory_name_mismatch_fails(tmp_path: Path) -> None:
    _write_skill(tmp_path, "directory-name", name="different-name")

    messages = _messages(tmp_path)

    assert any("directory-name" in message and "different-name" in message for message in messages)


def test_duplicate_name_fails(tmp_path: Path) -> None:
    first = _write_skill(tmp_path, "first", name="duplicate")
    second = _write_skill(tmp_path, "second", name="duplicate")

    messages = _messages(tmp_path)

    assert any(str(first.relative_to(tmp_path)) in message for message in messages)
    assert any(str(second.relative_to(tmp_path)) in message for message in messages)


def test_missing_description_fails(tmp_path: Path) -> None:
    _write_skill(tmp_path, "missing-description", description="")

    assert any("description" in message for message in _messages(tmp_path))


def test_unknown_mutation_scope_fails(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "unknown-scope",
        mutation_scopes=("production",),
        approval="required-before-mutation",
        body="# Unknown\n\n## Approval Gate\n",
    )

    assert any("unknown mutation scope" in message for message in _messages(tmp_path))


def test_read_only_contract_rejects_required_approval(tmp_path: Path) -> None:
    _write_skill(tmp_path, "read-only", approval="required-before-mutation")

    assert any("empty mutation_scopes" in message for message in _messages(tmp_path))


def test_mutating_contract_rejects_not_required_approval(tmp_path: Path) -> None:
    _write_skill(tmp_path, "mutating", mutation_scopes=("repository",))

    assert any("required-before-mutation" in message for message in _messages(tmp_path))


def test_dangling_local_reference_fails(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "source",
        description="Use `missing-skill` for follow-up work.",
    )

    assert any(
        "missing-skill" in message and "dangling" in message for message in _messages(tmp_path)
    )


def test_allowed_external_reference_passes(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "source",
        description="Use `observability-platform-admin` for the company-wide view.",
    )

    assert validate_contracts(tmp_path) == []


def test_direct_mcp_tool_reference_fails(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "source",
        body="# Source\n\nCall `mcp__retired__query` for data.\n",
    )

    assert any(
        "mcp__retired__query" in message and ":9:" in message for message in _messages(tmp_path)
    )


def test_worktrees_are_not_scanned(tmp_path: Path) -> None:
    _write_skill(tmp_path, "source")
    worktree_skill = tmp_path / ".worktrees" / "copy" / ".agents" / "skills" / "broken" / "SKILL.md"
    worktree_skill.parent.mkdir(parents=True)
    worktree_skill.write_text("# Missing frontmatter\n", encoding="utf-8")

    assert validate_contracts(tmp_path) == []


def test_repository_skills_pass() -> None:
    assert validate_contracts(REPO_ROOT) == []
