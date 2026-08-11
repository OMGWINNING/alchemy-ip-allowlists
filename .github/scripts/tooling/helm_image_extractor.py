#! /usr/bin/env python3

# usage: uv run .github/scripts/tooling/helm_image_extractor.py --target-chart alchemy-observability-grafana

import argparse
import pathlib
import sys
import yaml
from loguru import logger


def extract_images_from_yaml(yaml_content: dict) -> set[str]:
    """Recursively extract container images from a Kubernetes manifest.

    Args:
        yaml_content: Parsed YAML content as a dictionary

    Returns:
        Set of container image strings found in the manifest
    """
    images = set()

    def recurse_extract(obj):
        """Recursively search for 'image' keys in nested structures."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "image" and isinstance(value, str):
                    images.add(value)
                else:
                    recurse_extract(value)
        elif isinstance(obj, list):
            for item in obj:
                recurse_extract(item)

    recurse_extract(yaml_content)
    return images


def extract_images_from_file(file_path: str | pathlib.Path) -> set[str]:
    """Extract all container images from a rendered Kubernetes YAML file.

    Args:
        file_path: Path to the rendered YAML file

    Returns:
        Set of unique container images found in the file
    """
    images = set()

    try:
        with open(file_path) as f:
            # rendered helm contains multiple YAML documents: ---
            for doc in yaml.safe_load_all(f):
                if doc:  # Skip empty documents
                    images.update(extract_images_from_yaml(doc))
    except Exception as e:
        logger.error(f"Error processing file {file_path}: {e}")

    return images


def extract_all_images(rendered_files: list[str]) -> set[str]:
    """Extract all unique images from multiple rendered YAML files.

    Args:
        rendered_files: List of paths to rendered YAML files

    Returns:
        Set of all unique container images found across all files
    """
    all_images = set()

    for file_path in rendered_files:
        logger.info(f"Extracting images from: {file_path}")
        file_images = extract_images_from_file(file_path)
        all_images.update(file_images)
        logger.debug(f"  Found {len(file_images)} image(s) in this file")

    return all_images


def main():
    parser = argparse.ArgumentParser(
        description="Extract images from Helm charts",
        epilog="""
        Examples:
          %(prog)s --target-chart alchemy-observability-grafana
        """,
    )
    parser.add_argument(
        "--target-chart",
        type=str,
        required=True,
        help="Target chart directory to extract images from",
    )
    args = parser.parse_args()

    chart_name = args.target_chart

    # Validate chart directory exists
    if not pathlib.Path(chart_name).is_dir():
        logger.error(f"Chart directory not found: {chart_name}")
        sys.exit(1)

    # Check if helm-render-output directory exists
    output_dir = pathlib.Path(chart_name) / "helm-render-output"
    if not output_dir.exists():
        logger.error(f"helm-render-output directory not found at {output_dir}")
        logger.error("Please render the chart first before extracting images")
        logger.error(
            f"uv run --project .github/scripts helm-renderer --target-repo . --target-chart {chart_name}"
        )
        sys.exit(1)

    logger.success(f"Found helm-render-output directory at: {output_dir}")

    # Collect all YAML files from helm-render-output directory recursively
    rendered_files = list(output_dir.rglob("*.yaml"))

    if not rendered_files:
        logger.warning(f"No YAML files found in {output_dir}")
        sys.exit(1)

    logger.info(f"Found {len(rendered_files)} rendered YAML file(s)")

    # Extract images from all rendered files
    logger.info("=" * 80)
    logger.info("Extracting container images from rendered files...")
    logger.info("=" * 80)

    all_images = extract_all_images([str(f) for f in rendered_files])

    if not all_images:
        logger.warning("No container images found in rendered files")
        return

    # Display results
    logger.info("=" * 80)
    logger.success(f"Found {len(all_images)} unique container image(s) in {chart_name}:")
    logger.info("=" * 80)

    # imagex.txt for trivy scan workflow
    with open("images.txt", "w") as f:
        for image in sorted(all_images):
            f.write(image + "\n")
            logger.info(f"  {image}")
    logger.success("Images written to images.txt")
    logger.info("=" * 80)
    logger.success("Image extraction complete!")


if __name__ == "__main__":
    main()
