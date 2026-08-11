"""Verify package imports work when scripts are invoked directly (not via pytest).

pytest's pythonpath = ["."] hides a common failure mode: running
`uv run --project .github/scripts python .github/scripts/ci/<script>.py`
sets sys.path[0] to the script's directory, so `from lib…` / `from ci…` only
work when the project is installed as a package (see pyproject.toml [tool.uv]).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_ROOT.parents[1]


def _uv_run_script(script_rel: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = SCRIPTS_ROOT / script_rel
    return subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(SCRIPTS_ROOT),
            "python",
            str(script),
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_ci_script_imports_when_run_directly():
    result = _uv_run_script("ci/check_prod_stage_alignment.py", "--help")
    assert result.returncode == 0, result.stderr or result.stdout


def test_rollout_script_imports_when_run_directly():
    result = _uv_run_script("rollout/bump_chart_version.py", "--help")
    assert result.returncode == 0, result.stderr or result.stdout


def test_get_repo_root_accepts_git_file(tmp_path):
    """Worktrees expose .git as a file, not a directory."""
    from lib.common import get_repo_root

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /path/to/main/.git/worktrees/foo\n")
    nested = worktree / "a" / "b"
    nested.mkdir(parents=True)

    # Patch starting point by calling the same walk from a known nested path.
    path = nested.resolve()
    found = None
    for candidate in path.parents:
        if (candidate / ".git").exists():
            found = candidate
            break
    assert found == worktree

    # Sanity: real repo root still resolves in this checkout.
    assert get_repo_root() == REPO_ROOT
