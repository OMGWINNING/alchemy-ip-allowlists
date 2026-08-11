#! /usr/bin/env python3
"""Update Docker Images to Latest Patch Versions

Extracts images from values.yaml and updates to latest patch versions.

Usage:
    .github/scripts/rollout/update_docker_images.py --chart core [--check] [--dry-run]
    .github/scripts/rollout/update_docker_images.py --update-all [--dry-run]
    .github/scripts/rollout/update_docker_images.py --chart core --values-file path/to/values.yaml
"""

import argparse
import pathlib
import re
from typing import NamedTuple

import requests
from loguru import logger

import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lib.common import CHARTS, BaseChartManager, add_common_arguments


class Image(NamedTuple):
    registry: str
    repository: str
    tag: str

    @property
    def major(self) -> int | None:
        m = re.search(r"v?(\d+)", self.tag)
        return int(m.group(1)) if m else None

    @property
    def minor(self) -> int | None:
        m = re.search(r"v?\d+\.(\d+)", self.tag)
        return int(m.group(1)) if m else None

    @property
    def patch(self) -> int | None:
        m = re.search(r"v?\d+\.\d+\.(\d+)", self.tag)
        return int(m.group(1)) if m else None


class ImageUpdater(BaseChartManager):
    def parse_image(self, img_str):
        image_part, tag = img_str.rsplit(":", 1) if ":" in img_str else (img_str, "latest")

        if "/" in image_part:
            first = image_part.split("/")[0]
            if "." in first or first == "localhost":
                registry, repository = first, image_part[len(first) + 1 :]
            else:
                registry, repository = "", image_part
        else:
            registry, repository = "", image_part

        return Image(registry, repository, tag)

    def extract_images(self, values_file):
        if not values_file.exists():
            return []

        with open(values_file) as f:
            content = f.read()

        images, seen = [], set()

        # Pattern 1: registry + repository + tag
        for registry, repo, tag in re.findall(
            r'registry:\s*["\']?([^"\'\s]+)["\']?\s*\n\s+repository:\s*["\']?([^"\'\s]+)["\']?\s*\n\s+tag:\s*["\']?([^"\'\s]+)["\']?',
            content,
            re.MULTILINE,
        ):
            key = f"{repo}:{tag}"
            if key not in seen:
                images.append(self.parse_image(f"{registry}/{repo}:{tag}"))
                seen.add(key)

        # Pattern 2: repository + tag (no separate registry)
        for repo, tag in re.findall(
            r'repository:\s*["\']?([^"\'\s]+)["\']?\s*\n\s+tag:\s*["\']?([^"\'\s]+)["\']?',
            content,
            re.MULTILINE,
        ):
            key = f"{repo}:{tag}"
            if key not in seen:
                images.append(self.parse_image(f"{repo}:{tag}"))
                seen.add(key)

        return images

    def get_tags(self, image):
        try:
            if image.registry == "quay.io":
                r = requests.get(
                    f"https://quay.io/api/v1/repository/{image.repository}/tag/",
                    params={"limit": 100},
                    timeout=10,
                )
                return [t["name"] for t in r.json().get("tags", [])]
            elif image.registry == "ghcr.io":
                r = requests.get(f"https://ghcr.io/v2/{image.repository}/tags/list", timeout=10)
                if r.status_code == 401:
                    return []
                r.raise_for_status()
                return r.json().get("tags", [])
            elif image.registry in ["registry.k8s.io", "gcr.io", "k8s.gcr.io"]:
                r = requests.get(
                    f"https://registry.k8s.io/v2/{image.repository}/tags/list",
                    timeout=10,
                )
                r.raise_for_status()
                return r.json().get("tags", [])
            else:  # Docker Hub - fetch multiple pages to get more tags
                url = f"https://hub.docker.com/v2/repositories/{'library/' if '/' not in image.repository else ''}{image.repository}/tags"
                all_tags = []
                page = 1
                max_pages = 5  # Fetch up to 500 tags

                while page <= max_pages:
                    r = requests.get(url, params={"page_size": 100, "page": page}, timeout=10)
                    r.raise_for_status()
                    data = r.json()
                    results = data.get("results", [])

                    if not results:
                        break

                    all_tags.extend([t["name"] for t in results])

                    # Check if there's a next page
                    if not data.get("next"):
                        break

                    page += 1

                logger.debug(
                    f"Fetched {len(all_tags)} tags from Docker Hub for {image.repository} ({page - 1} pages)"
                )
                return all_tags
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error fetching tags for {image.registry}/{image.repository}: {e}")
            return []
        except Exception as e:
            logger.error(f"Error fetching tags for {image.registry}/{image.repository}: {e}")
            return []

    def find_latest_patch(self, image):
        logger.info(f"Finding latest version for {image.repository}:{image.tag}")
        if image.major is None:
            logger.warning(
                f"Skipping {image.repository}:{image.tag} - not a valid semver (major={image.major})"
            )
            return None

        tags = self.get_tags(image)
        if not tags:
            logger.warning(f"No tags found for {image.registry}/{image.repository}")
            return None

        logger.info(f"Found {len(tags)} total tags for {image.repository}")

        # Find all versions with the same major version
        matching = []
        for tag in tags:
            parsed = self.parse_image(f"{image.repository}:{tag}")
            if (
                parsed.major == image.major
                and parsed.minor is not None
                and parsed.patch is not None
                and re.match(r"^v?\d+\.\d+\.\d+$", tag)
            ):
                matching.append(parsed)

        logger.info(
            f"Found {len(matching)} matching v{image.major}.x.x versions for {image.repository}:{image.tag}"
        )
        if matching:
            # Show top 5 latest versions
            top_versions = sorted(matching, key=lambda v: (v.minor, v.patch), reverse=True)[:5]
            logger.info(f"Latest available versions: {[m.tag for m in top_versions]}")

        if not matching:
            logger.warning(
                f"No matching v{image.major}.x.x versions found for {image.repository}:{image.tag}"
            )
            return None

        # Find the latest version by comparing (minor, patch)
        latest = max(matching, key=lambda v: (v.minor, v.patch))

        # Check if it's newer than current version
        current_version = (image.minor or 0, image.patch or 0)
        latest_version = (latest.minor, latest.patch)

        if latest_version > current_version:
            logger.success(f"✓ UPDATE: {image.repository}:{image.tag} → {latest.tag}")
            return latest.tag
        else:
            logger.info(f"✓ CURRENT: {image.repository}:{image.tag} is already latest")
        return None

    def update_values(self, values_file, updates):
        if not values_file.exists():
            return False

        with open(values_file) as f:
            content = f.read()

        original = content
        for repo, new_tag in updates.items():
            base = repo.split("/")[-1]
            for pattern, repl in [
                (
                    rf'(repository:\s*["\']?{re.escape(repo)}["\']?\s*\n\s+tag:\s*["\'])([^"\']+)(["\'])',
                    rf"\g<1>{new_tag}\g<3>",
                ),
                (
                    rf'(repository:\s*["\']?{re.escape(repo)}["\']?\s*\n\s+tag:\s*)([^\s\n]+)',
                    rf'\g<1>"{new_tag}"',
                ),
                (
                    rf'(repository:\s*["\']?{re.escape(base)}["\']?\s*\n\s+tag:\s*["\'])([^"\']+)(["\'])',
                    rf"\g<1>{new_tag}\g<3>",
                ),
                (
                    rf'(repository:\s*["\']?{re.escape(base)}["\']?\s*\n\s+tag:\s*)([^\s\n]+)',
                    rf'\g<1>"{new_tag}"',
                ),
            ]:
                new_content = re.sub(pattern, repl, content, flags=re.MULTILINE)
                if new_content != content:
                    content = new_content
                    break

        if content != original:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would update {values_file}")
            else:
                with open(values_file, "w") as f:
                    f.write(content)
                logger.success(f"Updated {values_file}")
            return True
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Update Docker images",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_arguments(parser)
    parser.add_argument("--update-all", action="store_true", help="Update all charts")
    parser.add_argument("--check", action="store_true", help="Check for updates without applying")
    parser.add_argument("--values-file", action="append", help="Additional values files to update")
    args = parser.parse_args()

    updater = ImageUpdater(dry_run=args.dry_run)
    repo_root = updater.repo_root
    charts = list(CHARTS.keys()) if args.update_all else ([args.chart] if args.chart else [])

    if not charts:
        parser.print_help()
        return

    for chart in charts:
        chart_dir = repo_root / CHARTS[chart]
        values_files = ["values.yaml"]
        if args.values_file:
            values_files.extend(args.values_file)

        # Collect all images
        all_images = []
        for vf in values_files:
            vf_path = chart_dir / vf if not pathlib.Path(vf).is_absolute() else pathlib.Path(vf)
            if not vf_path.exists():
                vf_path = repo_root / vf
            if vf_path.exists():
                all_images.extend(updater.extract_images(vf_path))

        if not all_images:
            logger.warning(f"No images found in {chart}")
            continue

        logger.info(f"\n{'=' * 60}")
        logger.info(f"{chart}: {len(all_images)} images")
        logger.info(f"{'=' * 60}")

        # Check for updates
        updates = {}
        updated_count = 0
        for img in all_images:
            latest = updater.find_latest_patch(img)
            if latest:
                updates[img.repository] = latest
                updated_count += 1
            else:
                updates[img.repository] = img.tag

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Summary: {updated_count}/{len(all_images)} images have updates")
        logger.info(f"{'=' * 60}\n")

        if args.check:
            continue

        # Update values files
        for vf in values_files:
            vf_path = chart_dir / vf if not pathlib.Path(vf).is_absolute() else pathlib.Path(vf)
            if not vf_path.exists():
                vf_path = repo_root / vf
            if vf_path.exists():
                updater.update_values(vf_path, updates)

    logger.success("Done!")


if __name__ == "__main__":
    main()
