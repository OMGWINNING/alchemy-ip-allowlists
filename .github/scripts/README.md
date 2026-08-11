# Observability Chart Management Scripts

## Layout

| Directory | Purpose |
|-----------|---------|
| `lib/` | Shared modules (`common.py`, `version_gate_findings.py`) |
| `ci/` | CI gate scripts (render gate, version gates, skill contracts) |
| `rollout/` | Chart dependency rollout automation |
| `tooling/` | Standalone utilities (AWS creds, image extraction) |
| `helm_tests/` | Chart render/scenario pytest suite + helm-renderer handler |
| `unit_tests/` | Pure-Python tests for `ci/` and `lib/` |

## Rollout Scripts

Three focused scripts for manual chart updates:

- **`rollout/update_helm_dependencies.py`** - Update helm dependencies to latest versions
- **`rollout/update_docker_images.py`** - Update container images to latest patches
- **`rollout/bump_chart_version.py`** - Bump chart versions (major/minor/patch)

Each script supports `--dry-run` to preview changes.

## Automated Rollout Workflow

**`rollout/create_update_prs.py`** orchestrates dependency updates via GitHub Actions.

Separates **shards** from **core+agents** for independent rollout control.

### Trigger Workflow

Go to: **Actions → Create Dependency Update PRs → Run workflow**

### What It Does

Creates 12 numbered PRs in sequence:

1. **Update dependencies** - Updates helm deps & images for all charts
2. **Stage: shards** - Deploy shards to stage
3. **Stage: core+agents** - Deploy core+agents to stage
4. **Prod usw2: shards** - Deploy shards to usw2
5. **Prod usw2: core+agents** - Deploy core+agents to usw2
6. **Prod euc1,euc2: shards** - Deploy shards to Europe
7. **Prod euc1,euc2: core+agents** - Deploy core+agents to Europe
8. **Prod apse1: shards** - Deploy shards to Asia Pacific
9. **Prod apse1: core+agents** - Deploy core+agents to Asia Pacific
10. **Prod use1: shards** - Deploy shards to final US region
11. **Prod use1: core+agents** - Deploy core+agents to final US region
12. **Snowflake** - Updates production snowflake config

### Merge Process

Merge PRs **in numbered order**. Each PR is signed and titled `[N/12]`.

For each region, merge shards first, verify, then merge core+agents.

## Manual Usage

```bash
# Check for updates
.github/scripts/rollout/update_helm_dependencies.py --check
.github/scripts/rollout/bump_chart_version.py --chart core --show

# Update a chart
.github/scripts/rollout/update_helm_dependencies.py --chart core --download
.github/scripts/rollout/update_docker_images.py --chart core
.github/scripts/rollout/bump_chart_version.py --chart core --patch

# Review
git diff
```

## Configuration

- **Rollout order**: Defined in `rollout/create_update_prs.py` (usw2 → euc1,euc2 → apse1 → use1)
- **Chart definitions**: Defined in each script's `CHARTS` constant
