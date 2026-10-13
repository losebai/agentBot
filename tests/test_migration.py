"""Exercise migration file boundaries and overwrite protection without external services."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "tools" / "migrate_chat.py"


class MigrationTests(unittest.TestCase):
    """Migrations must copy only declared artifacts and preserve existing targets."""

    def run_migration(self, source: Path, destination: Path) -> subprocess.CompletedProcess:
        """Invoke the migration CLI against temporary, explicitly selected directories."""
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(source), str(destination)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_copies_sources_and_fixtures_without_environment_or_caches(self):
        """Secrets and generated artifacts never enter the migration manifest."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            for name in (
                "src/chat/example.py",
                "src/chat/resources/workflow.json",
                "tests/unit/test_example.py",
                "tests/fixtures/example.json",
                "sql/schema.sql",
                ".env",
                "src/chat/__pycache__/cached.py",
                "tests/unit/test_gitlab_ci.py",
                "tests/unit/test_deployment_config.py",
            ):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("example", encoding="utf-8")
            result = self.run_migration(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((destination / "src/chat/example.py").is_file())
            self.assertTrue((destination / "tests/fixtures/example.json").is_file())
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / "src/chat/__pycache__").exists())
            manifest = json.loads(
                (destination / "docs/migration/source-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["files"]), 5)
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["files"]))

    def test_conflicting_destination_is_preserved(self):
        """A repeated migration cannot overwrite locally changed source code."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "old", root / "new"
            for directory, content in ((source, "original"), (destination, "local edit")):
                path = directory / "src/chat/example.py"
                path.parent.mkdir(parents=True)
                path.write_text(content, encoding="utf-8")
            result = self.run_migration(source, destination)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                (destination / "src/chat/example.py").read_text(), "local edit"
            )


if __name__ == "__main__":
    unittest.main()
