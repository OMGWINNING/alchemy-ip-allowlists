from __future__ import annotations

from pathlib import Path

from ci.check_certificate_expiry_monitoring import run_check, validate_config


def write_core_config(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / "helm" / "remote-clusters" / "prod" / "use1" / "ovh" / name / "core"
    path.mkdir(parents=True)
    config = path / "config.yaml"
    config.write_text(content)
    return config


def test_accepts_both_explicit_k3s_modes(tmp_path: Path):
    fallback = write_core_config(
        tmp_path,
        "fallback",
        """provider: ovh
certificateExpiryMonitoringMode: apiserver-fallback
patches:
- no-x509-exporter.yaml
""",
    )
    exporter = write_core_config(
        tmp_path,
        "exporter",
        """provider: tsw
certificateExpiryMonitoringMode: x509-exporter
patches:
- scrape-standard.yaml
""",
    )

    assert validate_config(fallback) == []
    assert validate_config(exporter) == []
    assert run_check(tmp_path)[0] == 0


def test_accepts_exporter_mode_without_patches(tmp_path: Path):
    exporter = write_core_config(
        tmp_path,
        "exporter-without-patches",
        """provider: ovh
certificateExpiryMonitoringMode: x509-exporter
""",
    )

    assert validate_config(exporter) == []


def test_rejects_missing_mode(tmp_path: Path):
    config = write_core_config(
        tmp_path,
        "missing",
        """provider: ovh
patches:
- scrape-standard.yaml
""",
    )

    messages = [violation.message for violation in validate_config(config)]
    assert any("certificateExpiryMonitoringMode" in message for message in messages)


def test_rejects_fallback_mode_without_the_upstream_alert_patch(tmp_path: Path):
    config = write_core_config(
        tmp_path,
        "missing-patch",
        """provider: ovh
certificateExpiryMonitoringMode: apiserver-fallback
patches:
- scrape-standard.yaml
""",
    )

    assert [violation.message for violation in validate_config(config)] == [
        "apiserver-fallback requires no-x509-exporter.yaml"
    ]


def test_rejects_conflicting_fallback_patch(tmp_path: Path):
    config = write_core_config(
        tmp_path,
        "conflict",
        """provider: ovh
certificateExpiryMonitoringMode: x509-exporter
patches:
- no-x509-exporter.yaml
""",
    )

    assert [violation.message for violation in validate_config(config)] == [
        "x509-exporter must not include no-x509-exporter.yaml"
    ]


def test_ignores_non_k3s_cores(tmp_path: Path):
    config = write_core_config(
        tmp_path,
        "eks",
        """provider: aws
patches:
- scrape-standard.yaml
""",
    )

    assert validate_config(config) == []
