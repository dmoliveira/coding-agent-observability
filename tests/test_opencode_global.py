"""Global OpenCode installation preserves configuration and private credentials."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import opencode_global


class GlobalOpenCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.credentials = {
            "publicKey": "pk-lf-private-test",
            "secretKey": "sk-lf-private-test",
            "baseUrl": "http://127.0.0.1:3000",
        }
        environment = mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config),
                "LANGFUSE_PUBLIC_KEY": "pk-lf-unrelated",
                "LANGFUSE_SECRET_KEY": "sk-lf-unrelated",
                "LANGFUSE_BASE_URL": "https://unrelated.invalid",
                "AI_OBSERVATORY_URL": "http://127.0.0.1:3000",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write_credentials(self) -> Path:
        directory = self.config / "ai-observatory"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "opencode-global.json"
        opencode_global.write_new(path, json.dumps(self.credentials))
        return path

    def fake_build(self, path: Path) -> None:
        (path / "index.js").write_text("export default {}\n")
        marker = {
            "version": 1,
            "source": opencode_global.source_hashes(),
            "files": opencode_global.file_hashes(path),
        }
        opencode_global.write_new(path / opencode_global.MARKER, json.dumps(marker))

    def test_install_is_idempotent_and_preserves_global_settings(self) -> None:
        self.write_credentials()
        opencode = self.config / "opencode"
        opencode.mkdir()
        settings = opencode / "opencode.json"
        settings.write_text('{"plugins":["other"],"model":"keep"}\n')
        original = settings.read_bytes()
        output = io.StringIO()
        with (
            mock.patch.object(opencode_global, "verify_key") as verify,
            mock.patch.object(
                opencode_global, "make_plugin", side_effect=self.fake_build
            ) as build,
            contextlib.redirect_stdout(output),
        ):
            opencode_global.connect()
            opencode_global.connect()
        self.assertEqual(settings.read_bytes(), original)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(
            verify.call_args_list,
            [
                mock.call(
                    "http://127.0.0.1:3000", "pk-lf-private-test", "sk-lf-private-test"
                ),
                mock.call(
                    "http://127.0.0.1:3000", "pk-lf-private-test", "sk-lf-private-test"
                ),
            ],
        )
        self.assertNotIn("sk-lf-private-test", output.getvalue())
        self.assertNotIn("unrelated", output.getvalue())
        self.assertIn(
            "export { default }",
            (opencode / "plugins" / "ai-observatory.js").read_text(),
        )

    def test_existing_plugin_entry_is_never_overwritten(self) -> None:
        self.write_credentials()
        plugins = self.config / "opencode" / "plugins"
        plugins.mkdir(parents=True)
        entry = plugins / "ai-observatory.js"
        entry.write_text("user content")
        with (
            mock.patch.object(opencode_global, "verify_key"),
            mock.patch.object(
                opencode_global, "make_plugin", side_effect=self.fake_build
            ),
            self.assertRaisesRegex(ValueError, "left unchanged"),
        ):
            opencode_global.connect()
        self.assertEqual(entry.read_text(), "user content")

    def test_existing_managed_package_changes_are_not_overwritten(self) -> None:
        self.write_credentials()
        with (
            mock.patch.object(opencode_global, "verify_key"),
            mock.patch.object(
                opencode_global, "make_plugin", side_effect=self.fake_build
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            opencode_global.connect()
            index = next(
                (self.config / "ai-observatory").glob("opencode-plugin-*/index.js")
            )
            index.write_text("user modification")
            with self.assertRaisesRegex(ValueError, "left unchanged"):
                opencode_global.connect()
            self.assertEqual(index.read_text(), "user modification")

    def test_unrelated_environment_keys_are_not_used(self) -> None:
        with (
            mock.patch.object(opencode_global.sys.stdin, "isatty", return_value=False),
            mock.patch.object(opencode_global, "verify_key") as verify,
            self.assertRaisesRegex(ValueError, "No private credentials"),
        ):
            opencode_global.connect()
        verify.assert_not_called()
        self.assertFalse(opencode_global.private_config_path().exists())

    def test_interrupted_build_can_be_retried_without_deleting_any_files(self) -> None:
        self.write_credentials()

        def fail_build(path: Path) -> None:
            (path / "partial").write_text("preserve")
            raise ValueError("synthetic failure")

        with (
            mock.patch.object(opencode_global, "verify_key"),
            mock.patch.object(opencode_global, "make_plugin", side_effect=fail_build),
            self.assertRaisesRegex(ValueError, "synthetic failure"),
        ):
            opencode_global.connect()
        partial = next(
            (self.config / "ai-observatory").glob("opencode-plugin-*/partial")
        )
        with (
            mock.patch.object(opencode_global, "verify_key"),
            mock.patch.object(
                opencode_global, "make_plugin", side_effect=self.fake_build
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            opencode_global.connect()
        self.assertEqual(partial.read_text(), "preserve")
        self.assertTrue(
            (self.config / "opencode" / "plugins" / "ai-observatory.js").is_file()
        )

    def test_url_must_match_the_runtime_canonical_form(self) -> None:
        for url in (
            "http://127.0.0.1:080",
            "http://127.0.0.1:80",
            "https://host.example.ts.net:443",
            "https://HOST.example.ts.net:15443",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                opencode_global.validate_global_url(url)
        self.assertEqual(
            opencode_global.validate_global_url("https://host.example.ts.net:15443"),
            "https://host.example.ts.net:15443",
        )

    def test_relative_config_root_is_rejected(self) -> None:
        with (
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/config"}),
            self.assertRaisesRegex(ValueError, "absolute"),
        ):
            opencode_global.config_root()

    def test_explicit_different_destination_does_not_reuse_existing_key(self) -> None:
        self.write_credentials()
        with (
            mock.patch.dict(
                os.environ, {"AI_OBSERVATORY_URL": "http://127.0.0.1:4321"}
            ),
            mock.patch.object(opencode_global, "verify_key") as verify,
            self.assertRaisesRegex(ValueError, "different Langfuse destination"),
        ):
            opencode_global.connect()
        verify.assert_not_called()

    def test_credentials_file_must_be_private_and_not_a_symlink(self) -> None:
        path = self.write_credentials()
        self.assertEqual(opencode_global.read_private_json(path), self.credentials)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "not private"):
            opencode_global.read_private_json(path)
        path.unlink()
        path.symlink_to(self.root / "elsewhere")
        with self.assertRaisesRegex(ValueError, "safely read"):
            opencode_global.read_private_json(path)


if __name__ == "__main__":
    unittest.main()
