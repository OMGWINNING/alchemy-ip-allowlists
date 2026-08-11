"""Unit tests for check_prod_stage_alignment.py pure helpers."""

from __future__ import annotations

from ci.check_prod_stage_alignment import prod_pin_satisfied_by_stage  # noqa: E402


def test_prod_pin_satisfied_by_exact_stage_match():
    assert prod_pin_satisfied_by_stage("0.0.29", {"0.0.29", "0.0.31"})


def test_prod_pin_satisfied_when_stage_moved_ahead():
    assert prod_pin_satisfied_by_stage("0.0.29", {"0.0.31"})


def test_prod_pin_not_satisfied_when_ahead_of_stage():
    assert not prod_pin_satisfied_by_stage("0.0.31", {"0.0.29"})


def test_prod_pin_not_satisfied_when_stage_has_no_reachable_version():
    assert not prod_pin_satisfied_by_stage("0.0.31", set())
