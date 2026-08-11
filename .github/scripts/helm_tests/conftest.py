#!/usr/bin/env python3
"""Pytest hooks for helm_tests (chart render/scenario suites).

Optionally fails the suite when tests are skipped. A skip here means the
helm-renderer step did not produce an expected rendered output, which should
be a hard error in CI rather than a silent pass.

    PYTEST_FAIL_ON_SKIP=1 uv run --project .github/scripts pytest \
      .github/scripts/helm_tests/integration/<chart>/
"""

import os
import sys

import pytest


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run if any tests were skipped and PYTEST_FAIL_ON_SKIP is set."""
    if os.environ.get("PYTEST_FAIL_ON_SKIP", "").lower() not in ("1", "true", "yes"):
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return

    skipped_count: int = len(reporter.stats.get("skipped", []))
    if skipped_count > 0:
        print(
            f"\n❌ ERROR: {skipped_count} test(s) were skipped. "
            "This is treated as a failure when PYTEST_FAIL_ON_SKIP=1 "
            "(usually means helm-renderer did not produce an expected output).",
            file=sys.stderr,
        )
        session.exitstatus = 1
