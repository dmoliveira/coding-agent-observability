"""OpenCode integration keeps credentials private and tracing opt-in."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import opencode


class OpenCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        patch = mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "AI_OBSERVATORY_URL": "http://127.0.0.1:3000",
                "LANGFUSE_PUBLIC_KEY": "pk-lf-public-test",
                "LANGFUSE_SECRET_KEY": "sk-lf-secret-test",
            },
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_connect_is_opt_in_and_preserves_secrets(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(opencode, "verify_key"),
            contextlib.redirect_stdout(output),
        ):
            opencode.connect(self.project)
        self.assertNotIn("sk-lf-secret-test", output.getvalue())
        file = opencode.credentials_path(self.project)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(
            opencode.read_credentials(file)["LANGFUSE_BASE_URL"],
            "http://127.0.0.1:3000",
        )
        config = json.loads((self.project / ".opencode" / "opencode.json").read_text())
        self.assertEqual(config["plugins"], ["{env:AI_OBSERVATORY_PLUGIN}"])
        self.assertNotIn("sk-lf-secret-test", json.dumps(config))
        with self.assertRaisesRegex(ValueError, "already exist"):
            opencode.connect(self.project)

    def test_rejects_public_and_unencrypted_nonlocal_destinations(self) -> None:
        for url in (
            "https://cloud.langfuse.com:443",
            "http://host.example.ts.net:3000",
            "http://192.168.1.1:3000",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                opencode.validate_url(url)
        self.assertEqual(
            opencode.validate_url("https://node.example.ts.net:15443"),
            "https://node.example.ts.net:15443",
        )

    def test_existing_project_config_is_not_overwritten(self) -> None:
        config = self.project / ".opencode" / "opencode.jsonc"
        config.parent.mkdir()
        config.write_text("// user settings\n{}")
        with self.assertRaisesRegex(ValueError, "already exists"):
            opencode.connect(self.project)
        self.assertEqual(config.read_text(), "// user settings\n{}")
        self.assertFalse(opencode.credentials_path(self.project).exists())

    def test_existing_jsonc_with_plugin_gate_can_be_connected(self) -> None:
        config = self.project / ".opencode" / "opencode.jsonc"
        config.parent.mkdir()
        original = '// keep my settings\n{"plugins":["{env:AI_OBSERVATORY_PLUGIN}"]}\n'
        config.write_text(original)
        with mock.patch.object(opencode, "verify_key"):
            opencode.connect(self.project)
        self.assertEqual(config.read_text(), original)
        self.assertTrue(opencode.credentials_path(self.project).is_file())
        self.assertTrue(
            opencode.has_plugin_gate(opencode.project_configs(config.parent))
        )

    def test_project_launcher_disables_global_adapter_to_avoid_duplicates(self) -> None:
        with mock.patch.object(opencode, "verify_key"):
            opencode.connect(self.project)
        with (
            mock.patch.object(opencode, "project_path", return_value=self.project),
            mock.patch.object(opencode, "verify_key"),
            mock.patch.object(opencode.os, "chdir"),
            mock.patch.object(opencode.os, "execvpe") as execute,
        ):
            opencode.main("run")
        command, args, environment = execute.call_args.args
        self.assertEqual(command, "opencode")
        self.assertIn("--standalone", args)
        self.assertEqual(environment["AI_OBSERVATORY_DISABLED"], "1")


if __name__ == "__main__":
    unittest.main()
