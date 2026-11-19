"""Backup safety checks; no Docker state is modified by these tests."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backup import manage
from setup import initialize


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def nested_tar(
        self, name: str, *, kind: bytes = tarfile.REGTYPE, link: str = ""
    ) -> Path:
        result = self.root / "volume.tar"
        with tarfile.open(result, "w") as archive:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            if kind == tarfile.REGTYPE:
                payload = b"test data"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            else:
                archive.addfile(info)
        return result

    def test_rejects_traversal_and_links(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe volume path"):
            manage.safe_nested_tar(self.nested_tar("../outside"))
        with self.assertRaisesRegex(ValueError, "unsafe volume link"):
            manage.safe_nested_tar(
                self.nested_tar("link", kind=tarfile.SYMTYPE, link="../outside")
            )
        with self.assertRaisesRegex(ValueError, "unsafe volume link"):
            manage.safe_nested_tar(
                self.nested_tar("link", kind=tarfile.LNKTYPE, link="../outside")
            )

    def test_accepts_regular_volume_content(self) -> None:
        manage.safe_nested_tar(self.nested_tar("./safe/file"))

    def test_accepts_clickhouse_relative_links(self) -> None:
        manage.safe_nested_tar(
            self.nested_tar(
                "data/default/traces", kind=tarfile.SYMTYPE, link="../../store/id"
            )
        )

    def test_rejects_link_chains_and_entries_beneath_links(self) -> None:
        path = self.root / "volume.tar"
        for second_name, second_link in (
            ("other", "link/../outside"),
            ("link/file", ""),
        ):
            with self.subTest(name=second_name), tarfile.open(path, "w") as archive:
                first = tarfile.TarInfo("link")
                first.type, first.linkname = tarfile.SYMTYPE, "dir"
                archive.addfile(first)
                second = tarfile.TarInfo(second_name)
                if second_link:
                    second.type, second.linkname = tarfile.SYMTYPE, second_link
                archive.addfile(second)
            with self.assertRaisesRegex(ValueError, "volume link"):
                manage.safe_nested_tar(path)

    def test_restore_never_overwrites_existing_config(self) -> None:
        config = self.root / ".env"
        config.write_text("existing user data")
        archive = self.root / "backup.tar.gz"
        archive.touch()
        with (
            mock.patch.object(manage, "CONFIG", config),
            mock.patch.object(manage, "docker") as docker,
        ):
            with self.assertRaisesRegex(ValueError, "fresh installation"):
                manage.restore(archive)
            docker.assert_not_called()
        self.assertEqual(config.read_text(), "existing user data")

    def test_publish_config_is_complete_and_never_replaces_a_file(self) -> None:
        config = self.root / ".env"
        with mock.patch.object(manage, "CONFIG", config):
            manage.publish_config({"KEY": "complete"})
            self.assertEqual(config.read_text(), "KEY=complete\n")
            with self.assertRaises(FileExistsError):
                manage.publish_config({"KEY": "replacement"})
            self.assertEqual(config.read_text(), "KEY=complete\n")
            self.assertEqual(list(self.root.glob(".env.restore-*")), [])

    def test_failed_publish_never_leaves_partial_config(self) -> None:
        config = self.root / ".env"
        with (
            mock.patch.object(manage, "CONFIG", config),
            mock.patch.object(manage.os, "fsync", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            manage.publish_config({"KEY": "content"})
        self.assertFalse(config.exists())
        self.assertEqual(list(self.root.glob(".env.restore-*")), [])

    def test_backup_rejects_running_volumes(self) -> None:
        running = mock.Mock(stdout="container-id\n")
        with (
            mock.patch.object(manage, "docker", side_effect=[mock.Mock(), running]),
            self.assertRaisesRegex(ValueError, "in use"),
        ):
            manage.require_stopped_volumes("aiobs-123456abcdef")

    def test_backup_rejects_interrupted_start(self) -> None:
        marker = self.root / ".ai-observatory.starting"
        marker.touch()
        with (
            mock.patch.object(manage, "ROOT", self.root),
            mock.patch.object(manage, "docker") as docker,
            self.assertRaisesRegex(ValueError, "make down"),
        ):
            manage.require_stopped_volumes("aiobs-123456abcdef")
        docker.assert_not_called()

    def test_restore_temporary_secrets_are_git_ignored(self) -> None:
        if not (ROOT / ".git").exists() or not shutil.which("git"):
            self.skipTest("Git checkout not available")
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", ".env.restore-example"],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_backup_config_validation_rejects_unsafe_archive(self) -> None:
        env = self.root / ".env"
        with (
            mock.patch.object(initialize, "CONFIG", env),
            mock.patch.dict(
                os.environ, {"AI_OBSERVATORY_ADMIN_PASSWORD": "safe_password_123456"}
            ),
        ):
            initialize.initialize()
        destination = self.root / "unpacked"
        destination.mkdir()
        archive = self.root / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            info = tarfile.TarInfo("../config.env")
            info.size = 1
            output.addfile(info, io.BytesIO(b"x"))
        with self.assertRaisesRegex(ValueError, "missing, duplicate, or unexpected"):
            manage.unpack_archive(archive, destination)
        self.assertFalse((self.root / "config.env").exists())

    def test_restore_rejects_changed_service_images(self) -> None:
        parts = self.root / "parts"
        parts.mkdir()
        config = parts / "config.env"
        with (
            mock.patch.object(initialize, "CONFIG", config),
            mock.patch.dict(
                os.environ, {"AI_OBSERVATORY_ADMIN_PASSWORD": "safe_password_123456"}
            ),
        ):
            initialize.initialize()
        files = [config]
        for volume in manage.VOLUMES:
            path = parts / f"{volume}.tar"
            with tarfile.open(path, "w"):
                pass
            files.append(path)
        manifest = {
            "format": manage.FORMAT,
            "langfuse_version": manage.VERSION,
            "architecture": "test-arch",
            "images": ["postgres:old"],
            "sha256": {file.name: manage.digest(file) for file in files},
        }
        manifest_file = parts / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))
        source = self.root / "snapshot.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            for file in [manifest_file, *files]:
                archive.add(file, arcname=file.name)
        destination = self.root / "unpacked"
        destination.mkdir()
        with (
            mock.patch.object(manage, "architecture", return_value="test-arch"),
            mock.patch.object(manage, "image_versions", return_value=["postgres:new"]),
            self.assertRaisesRegex(ValueError, "image versions"),
        ):
            manage.unpack_archive(source, destination)

        # Format 1 was used only by pre-release development snapshots; it did
        # not record service images and must not be mistaken for format 2.
        manifest["format"] = 1
        del manifest["images"]
        manifest_file.write_text(json.dumps(manifest))
        with tarfile.open(source, "w:gz") as archive:
            for file in [manifest_file, *files]:
                archive.add(file, arcname=file.name)
        legacy_destination = self.root / "legacy-unpacked"
        legacy_destination.mkdir()
        with self.assertRaisesRegex(ValueError, "format is unsupported"):
            manage.unpack_archive(source, legacy_destination)


if __name__ == "__main__":
    unittest.main()
