#!/usr/bin/env python3
"""Setup AWS SSO credentials and CodeArtifact authentication for helm-renderer."""

import subprocess
import sys
import shlex
import os
from pathlib import Path
from loguru import logger

# AWS SSO Configuration
SSO_START_URL: str = "https://d-92677ddf13.awsapps.com/start/#"
SSO_REGION: str = "us-west-2"
CODEARTIFACT_REGION: str = "us-east-1"
CODEARTIFACT_DOMAIN: str = "cloud-infra-tools"
CODEARTIFACT_DOMAIN_OWNER: str = "209202477790"


def detect_aws_profile() -> str | None:
    """
    Detect the AWS profile to use.

    Priority:
    1. AWS_PROFILE environment variable
    2. Profile with sso_account_id matching CodeArtifact domain owner (209202477790)
    3. 'default' if it exists
    """
    # Check environment variable
    if profile_env := os.environ.get("AWS_PROFILE"):
        logger.info(f"Using AWS_PROFILE from environment: {profile_env}")
        return profile_env

    # Parse config file for profiles with matching sso_account_id
    config_file: Path = Path.home() / ".aws" / "config"
    if not config_file.exists():
        logger.warning("AWS config file not found at ~/.aws/config")
        return None

    matching_profiles: list[str] = []
    all_profiles: list[str] = []
    current_profile: str | None = None

    with open(config_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("[profile "):
                current_profile = line[9:-1]
                all_profiles.append(current_profile)
            elif line.startswith("[default]"):
                current_profile = "default"
                all_profiles.append("default")
            elif line.startswith("sso_account_id") and current_profile:
                account_id: str = line.split("=")[1].strip()
                if account_id == CODEARTIFACT_DOMAIN_OWNER:
                    matching_profiles.append(current_profile)

    if matching_profiles:
        selected: str = matching_profiles[0]
        logger.info(f"Using profile '{selected}' (sso_account_id={CODEARTIFACT_DOMAIN_OWNER})")
        return selected

    if "default" in all_profiles:
        logger.info("Using default profile")
        return "default"

    logger.error(
        f"No AWS profile found with sso_account_id={CODEARTIFACT_DOMAIN_OWNER}. "
        f"Please add a profile to ~/.aws/config with: sso_account_id = {CODEARTIFACT_DOMAIN_OWNER}"
    )
    return None


def is_sso_logged_in(profile: str) -> bool:
    """Check if AWS SSO session is valid for the given profile."""
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--profile", profile],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            logger.info(f"SSO session valid for profile '{profile}'")
            return True
    except subprocess.TimeoutExpired:
        logger.warning("AWS credential check timed out")
    except FileNotFoundError:
        logger.error("AWS CLI not found. Please install it first.")
        sys.exit(1)

    return False


def login_sso(profile: str) -> bool:
    """Login to AWS SSO."""
    logger.info(f"Starting AWS SSO login for profile '{profile}'...")
    logger.info(f"SSO Start URL: {SSO_START_URL}")
    logger.info(f"SSO Region: {SSO_REGION}")

    try:
        subprocess.run(
            ["aws", "sso", "login", "--profile", profile],
            check=True,
        )
        logger.info("SSO login successful")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"SSO login failed: {e}")
        return False
    except FileNotFoundError:
        logger.error("AWS CLI not found. Please install it first.")
        sys.exit(1)


def get_codeartifact_token(profile: str) -> str | None:
    """Get CodeArtifact authorization token."""
    logger.info(f"Fetching CodeArtifact token using profile '{profile}'...")

    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [
                "aws",
                "codeartifact",
                "get-authorization-token",
                "--domain",
                CODEARTIFACT_DOMAIN,
                "--domain-owner",
                CODEARTIFACT_DOMAIN_OWNER,
                "--region",
                CODEARTIFACT_REGION,
                "--query",
                "authorizationToken",
                "--output",
                "text",
                "--profile",
                profile,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        token: str = result.stdout.strip()
        if token:
            logger.info("Successfully obtained CodeArtifact token")
            return token
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to get CodeArtifact token: {e.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("CodeArtifact token request timed out")

    return None


def setup_credentials() -> int:
    """Setup AWS credentials for helm-renderer."""
    logger.info("Setting up AWS credentials for helm-renderer...")

    # Detect profile
    profile: str | None = detect_aws_profile()
    if not profile:
        logger.error(
            f"No AWS profile found with sso_account_id={CODEARTIFACT_DOMAIN_OWNER}. "
            "Set AWS_PROFILE to override: export AWS_PROFILE=<profile-name>"
        )
        return 1

    # Check if already logged in
    if not is_sso_logged_in(profile):
        logger.info("SSO session expired or not configured. Logging in...")
        if not login_sso(profile):
            logger.error("Failed to login to AWS SSO")
            return 1

    # Get CodeArtifact token
    token: str | None = get_codeartifact_token(profile)
    if not token:
        logger.error("Failed to obtain CodeArtifact token")
        return 1

    # Output shell export statements for eval
    # IMPORTANT: This script is designed to be consumed via eval "$(…)" in the parent shell.
    # stdout MUST contain only shell export statements (for eval).
    # All logging goes to stderr via loguru (safe for eval).
    # Do NOT add print() calls for debugging; use logger.* instead.
    print(f"export UV_INDEX_CLOUD_INFRA_TOOLS_PASSWORD={shlex.quote(token)}")
    print("export UV_INDEX_CLOUD_INFRA_TOOLS_USERNAME='aws'")

    logger.info("Credentials setup complete")
    return 0


if __name__ == "__main__":
    sys.exit(setup_credentials())
