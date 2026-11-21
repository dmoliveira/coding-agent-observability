"""Serialize state transitions across independent CLI processes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common


class LockTests(unittest.TestCase):
    def test_competing_process_cannot_acquire_report_query_lock(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
            common.report_query_lock(),
        ):
            code = (
                "import common,sys; from pathlib import Path; "
                "common.ROOT=Path(sys.argv[1]); "
                "ctx=common.report_query_lock(); ctx.__enter__()"
            )
            result = subprocess.run(
                [sys.executable, "-c", code, directory],
                env={"PYTHONPATH": str(ROOT / "scripts")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Another structural report query", result.stderr)

    def test_report_query_lock_refuses_symlink(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
        ):
            (Path(directory) / ".ai-observatory.report.lock").symlink_to(
                Path(directory) / "target"
            )
            with self.assertRaises(OSError), common.report_query_lock():
                pass

    def test_competing_process_cannot_acquire_maintenance_lock(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
        ):
            code = (
                "import common,sys; from pathlib import Path; "
                "common.ROOT=Path(sys.argv[1]); "
                "ctx=common.maintenance_lock(); ctx.__enter__()"
            )
            env = {"PYTHONPATH": str(ROOT / "scripts")}
            with common.maintenance_lock():
                result = subprocess.run(
                    [sys.executable, "-c", code, directory],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Another observatory maintenance command", result.stderr)
            with common.maintenance_lock():
                pass

    def test_lock_refuses_symlink(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
        ):
            (Path(directory) / ".ai-observatory.lock").symlink_to(
                Path(directory) / "target"
            )
            with self.assertRaises(OSError), common.maintenance_lock():
                pass

    def test_child_keeps_lock_when_parent_exits(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
        ):
            code = """
import common, os, subprocess, sys
from pathlib import Path
common.ROOT = Path(sys.argv[1])
with common.maintenance_lock():
    subprocess.Popen([sys.executable, '-c', "import sys; print('ready', flush=True); sys.stdin.read()"],
                     stdin=sys.stdin, stdout=sys.stdout, pass_fds=common.maintenance_fds())
    os._exit(0)
"""
            parent = subprocess.Popen(
                [sys.executable, "-c", code, directory],
                env={"PYTHONPATH": str(ROOT / "scripts")},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(parent.stdout.readline().strip(), "ready")
                parent.wait(timeout=10)
                with (
                    self.assertRaisesRegex(ValueError, "Another observatory"),
                    common.maintenance_lock(),
                ):
                    pass
            finally:
                parent.stdin.close()
                parent.stdout.read()
                parent.stdout.close()
                parent.stderr.close()
                parent.wait(timeout=10)
            with common.maintenance_lock():
                pass

    def test_failed_start_blocks_retry_until_successful_down(self) -> None:
        config = {"COMPOSE_PROJECT_NAME": "aiobs-123456abcdef"}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
        ):
            with (
                mock.patch.object(
                    common.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(1, "docker"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                common.run_compose(config, "up")
            self.assertTrue((Path(directory) / ".ai-observatory.starting").is_file())
            with self.assertRaisesRegex(ValueError, "make down"):
                common.run_compose(config, "up")
            with mock.patch.object(common.subprocess, "run"):
                common.run_compose(config, "down")
            self.assertFalse((Path(directory) / ".ai-observatory.starting").exists())


if __name__ == "__main__":
    unittest.main()
