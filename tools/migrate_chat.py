"""Copy the declared Chat source artifacts into an independent project."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EXCLUDED_TESTS = {"test_gitlab_ci.py", "test_deployment_config.py"}
EXCLUDED_DIRECTORIES = {"__pycache__", ".pytest_cache", ".venv", ".idea", "dist"}


def migrate(source: Path, destination: Path) -> int:
    """Preflight every target before copying; never overwrite differing content."""
    source, destination = source.resolve(strict=True), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination must be separate directories")
    files = []
    for folder, suffixes in (
        ("src", {".py", ".json"}),
        ("tests", {".py", ".json"}),
        ("sql", {".sql"}),
    ):
        for path in sorted((source / folder).rglob("*")):
            relative = path.relative_to(source)
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if EXCLUDED_DIRECTORIES.intersection(relative.parts):
                continue
            if folder == "tests" and path.name in EXCLUDED_TESTS:
                continue
            if source not in path.resolve().parents:
                raise ValueError("source artifact escapes the selected directory")
            target = destination / relative
            if destination not in target.resolve().parents:
                raise ValueError("target artifact escapes the selected directory")
            contents = path.read_bytes()
            if target.exists() and target.read_bytes() != contents:
                raise ValueError(f"refusing to overwrite changed target: {relative.as_posix()}")
            files.append((relative, contents, folder))
    manifest = []
    for relative, contents, purpose in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(contents)
        manifest.append(
            {
                "source": relative.as_posix(),
                "destination": relative.as_posix(),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "purpose": purpose,
            }
        )
    manifest_path = destination / "docs/migration/source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "source_directory": str(source),
                "files": manifest,
                "excluded_tests": sorted(EXCLUDED_TESTS),
                "exclusion_reason": (
                    "Old repository GitLab/Helm deployment configuration is not migrated."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(files)


def main() -> None:
    """Parse explicitly supplied directories and perform a controlled migration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        count = migrate(args.source, args.destination)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Migration refused: {error}\n")
    print(f"Migrated {count} declared artifacts.")


if __name__ == "__main__":
    main()
