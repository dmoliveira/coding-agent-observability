"""Security and idempotence checks for initial setup and Compose."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from common import load_config
from setup import initialize


class InitializeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"
        self.config_patch = mock.patch.object(initialize, "CONFIG", self.path)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "AI_OBSERVATORY_ADMIN_EMAIL": "test@example.invalid",
                "AI_OBSERVATORY_ADMIN_PASSWORD": "test_password_123456789",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_setup_creates_private_secrets_once(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(initialize.initialize(), 0)
        self.assertNotIn("test_password_123456789", output.getvalue())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        before = self.path.read_bytes()
        config = load_config(self.path)
        self.assertNotEqual(config["SALT"], config["NEXTAUTH_SECRET"])
        self.assertEqual(initialize.initialize(), 0)
        self.assertEqual(before, self.path.read_bytes())

    def test_rejects_weak_password_without_writing(self) -> None:
        os.environ["AI_OBSERVATORY_ADMIN_PASSWORD"] = "short"
        with self.assertRaises(ValueError):
            initialize.initialize()
        self.assertFalse(self.path.exists())

    def test_existing_insecure_file_is_not_overwritten(self) -> None:
        self.path.write_text("COMPOSE_PROJECT_NAME=aiobs-123456abcdef\n")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            initialize.initialize()
        self.assertEqual(
            self.path.read_text(), "COMPOSE_PROJECT_NAME=aiobs-123456abcdef\n"
        )

    def test_symlink_is_not_overwritten(self) -> None:
        target = Path(self.directory.name) / "target"
        target.write_text("preserve me")
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            initialize.initialize()
        self.assertEqual(target.read_text(), "preserve me")


@unittest.skipUnless(shutil.which("docker"), "Docker CLI not installed")
class ComposeTests(InitializeTests):
    def test_rendered_stack_publishes_only_loopback(self) -> None:
        initialize.initialize()
        config = load_config(self.path)
        env = os.environ.copy()
        for key in (*config, "COMPOSE_FILE", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES"):
            env.pop(key, None)
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(self.path),
                "-f",
                str(ROOT / "compose.yaml"),
                "config",
                "--format",
                "json",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        rendered = json.loads(result.stdout)
        self.assertNotIn("lmstudio-gateway", rendered["services"])
        self.assertNotIn(
            "LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST",
            rendered["services"]["web"]["environment"],
        )
        published = {
            name: service["ports"]
            for name, service in rendered["services"].items()
            if service.get("ports")
        }
        self.assertEqual(set(published), {"web", "minio"})
        self.assertTrue(
            all(
                port.get("host_ip") == "127.0.0.1"
                for ports in published.values()
                for port in ports
            )
        )
        self.assertEqual(
            rendered["services"]["web"]["environment"]["AUTH_DISABLE_SIGNUP"], "true"
        )
        self.assertEqual(
            rendered["services"]["web"]["environment"]["TELEMETRY_ENABLED"], "false"
        )

    def test_opt_in_lmstudio_overlay_preserves_named_volumes_and_limits_access(
        self,
    ) -> None:
        initialize.initialize()
        config = load_config(self.path)
        env = os.environ.copy()
        for key in (*config, "COMPOSE_FILE", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES"):
            env.pop(key, None)
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(self.path),
                "-f",
                str(ROOT / "compose.yaml"),
                "-f",
                str(ROOT / "compose.lmstudio.yaml"),
                "config",
                "--format",
                "json",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        services = json.loads(result.stdout)["services"]
        for name in ("web", "worker"):
            self.assertEqual(
                services[name]["environment"][
                    "LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST"
                ],
                "lmstudio-gateway",
            )
        gateway = services["lmstudio-gateway"]
        self.assertFalse(gateway.get("ports"))
        self.assertTrue(gateway["read_only"])
        self.assertEqual(gateway["cap_drop"], ["ALL"])
        self.assertTrue(gateway["volumes"][0]["read_only"])
        for name in ("postgres", "clickhouse", "redis", "minio"):
            self.assertTrue(
                all(v["type"] == "volume" for v in services[name]["volumes"])
            )


if __name__ == "__main__":
    unittest.main()
