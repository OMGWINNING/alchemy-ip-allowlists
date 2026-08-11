#!/usr/bin/env python3
"""Enforce certificate-expiry monitoring coverage for every K3s core.

OVH and TSW remote cores in this repository run K3s. Every such core must
declare exactly one certificate-expiry monitoring path in its ``config.yaml``:

* ``x509-exporter`` renders the granular X509 certificate rules.
* ``apiserver-fallback`` enables kube-prometheus-stack's upstream
  KubeClientCertificateExpiration rule for clusters without the exporter.

The check scans the whole inventory on every relevant PR. The declared mode is
intentional redundant state: the fallback patch encodes the implementation,
while this field makes the expected monitoring path auditable and reviewable.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

CHECK_NAME = "certificate-expiry-monitoring"
K3S_PROVIDERS = frozenset({"ovh", "tsw"})
VALID_MODES = frozenset({"x509-exporter", "apiserver-fallback"})
FALLBACK_PATCH = "no-x509-exporter.yaml"


@dataclass(frozen=True)
class Violation:
    path: Path
    message: str

    def emit(self) -> None:
        print(f"::error file={self.path}::{CHECK_NAME}: {self.message}")


def core_configs(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "helm" / "remote-clusters").glob("**/core/config.yaml"))


def validate_config(path: Path) -> list[Violation]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return [Violation(path, f"malformed YAML: {exc}")]

    if not isinstance(data, dict):
        return [Violation(path, "config must be a YAML mapping")]

    provider = data.get("provider")
    if provider not in K3S_PROVIDERS:
        return []

    mode = data.get("certificateExpiryMonitoringMode")
    patches = data.get("patches", [])
    if not isinstance(patches, list) or not all(isinstance(p, str) for p in patches):
        return [Violation(path, "K3s core must declare patches as a list of patch filenames")]

    violations: list[Violation] = []
    if mode not in VALID_MODES:
        expected = ", ".join(sorted(VALID_MODES))
        violations.append(
            Violation(
                path, f"certificateExpiryMonitoringMode must be one of {expected}; got {mode!r}"
            )
        )
    has_fallback_patch = FALLBACK_PATCH in patches
    if mode == "apiserver-fallback" and not has_fallback_patch:
        violations.append(Violation(path, f"apiserver-fallback requires {FALLBACK_PATCH}"))
    if mode == "x509-exporter" and has_fallback_patch:
        violations.append(Violation(path, f"x509-exporter must not include {FALLBACK_PATCH}"))
    return violations


def run_check(repo_root: Path) -> tuple[int, list[Violation]]:
    configs = core_configs(repo_root)
    violations = [violation for path in configs for violation in validate_config(path)]
    for violation in violations:
        violation.emit()

    checked = len(configs)
    print(f"validated {checked} remote core config(s); {len(violations)} violation(s)")
    if violations:
        print(f"::error::found {len(violations)} {CHECK_NAME} violation(s)")
    return (1 if violations else 0), violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        code, _ = run_check(args.repo_root.resolve())
        return code
    except Exception as exc:
        print(f"::error::{CHECK_NAME} gate crashed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
