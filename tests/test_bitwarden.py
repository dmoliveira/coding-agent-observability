"""Optional Bitwarden lookup preserves the private local fallback."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common
from setup import initialize

ITEM_ID = "12345678-1234-1234-1234-123456789abc"
FALLBACK = "fallback_password_123456"
VAULT_PASSWORD = "vault_password_123456789"


class BitwardenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "ADMIN_PASSWORD": FALLBACK,
            "COMPOSE_PROJECT_NAME": "aiobs-123456abcdef",
        }

    def test_unconfigured_does_not_call_cli_or_override_dotenv(self) -> None:
        with (
            mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "inherited"}, clear=True),
            mock.patch.object(common.subprocess, "run") as run,
        ):
            self.assertIsNone(common.bitwarden_password(self.config))
            self.assertNotIn("ADMIN_PASSWORD", common.compose_environment(self.config))
        run.assert_not_called()

    def test_selected_vault_password_is_used_without_changing_config(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout=f"{VAULT_PASSWORD}\r\n")
        warning = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"AI_OBSERVATORY_BITWARDEN_ITEM": ITEM_ID}),
            mock.patch.object(common.subprocess, "run", return_value=result) as run,
            contextlib.redirect_stderr(warning),
        ):
            password = common.bitwarden_password(self.config)
        self.assertEqual(password, VAULT_PASSWORD)
        self.assertEqual(self.config["ADMIN_PASSWORD"], FALLBACK)
        self.assertIn("existing Langfuse logins are not changed", warning.getvalue())
        self.assertNotIn(VAULT_PASSWORD, warning.getvalue())
        run.assert_called_once_with(
            ["bw", "get", "password", ITEM_ID, "--nointeraction"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=15,
        )

    def test_matching_password_confirms_source_without_revealing_it(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout=f"{FALLBACK}\n")
        warning = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"AI_OBSERVATORY_BITWARDEN_ITEM": ITEM_ID}),
            mock.patch.object(common.subprocess, "run", return_value=result),
            contextlib.redirect_stderr(warning),
        ):
            self.assertEqual(common.bitwarden_password(self.config), FALLBACK)
        self.assertIn("Using Bitwarden administrator password", warning.getvalue())
        self.assertNotIn(FALLBACK, warning.getvalue())

    def test_lookup_failures_fall_back_without_printing_cli_output(self) -> None:
        responses = (
            FileNotFoundError(),
            subprocess.CalledProcessError(1, "bw", output=VAULT_PASSWORD),
            subprocess.TimeoutExpired("bw", 15, output=VAULT_PASSWORD),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=f"{VAULT_PASSWORD}\n\n"),
            subprocess.CompletedProcess([], 0, stdout="contains spaces and symbols!"),
        )
        for response in responses:
            warning = io.StringIO()
            with (
                self.subTest(response_type=type(response).__name__),
                mock.patch.dict(os.environ, {"AI_OBSERVATORY_BITWARDEN_ITEM": ITEM_ID}),
                mock.patch.object(common.subprocess, "run", side_effect=[response]),
                contextlib.redirect_stderr(warning),
            ):
                self.assertIsNone(common.bitwarden_password(self.config))
            self.assertIn("using .env fallback", warning.getvalue())
            self.assertNotIn(VAULT_PASSWORD, warning.getvalue())

    def test_invalid_item_id_falls_back_without_running_cli(self) -> None:
        for item in ("not-a-uuid", "--session=secret", ITEM_ID + "\n"):
            warning = io.StringIO()
            with (
                self.subTest(item=item),
                mock.patch.dict(os.environ, {"AI_OBSERVATORY_BITWARDEN_ITEM": item}),
                mock.patch.object(common.subprocess, "run") as run,
                contextlib.redirect_stderr(warning),
            ):
                self.assertIsNone(common.bitwarden_password(self.config))
            run.assert_not_called()
            self.assertIn("using .env fallback", warning.getvalue())
            self.assertNotIn(item, warning.getvalue())

    def test_compose_does_not_inherit_bitwarden_or_admin_credentials(self) -> None:
        inherited = {
            "ADMIN_PASSWORD": "untrusted_inherited_value",
            "AI_OBSERVATORY_ADMIN_PASSWORD": "setup_secret",
            "AI_OBSERVATORY_BITWARDEN_ITEM": ITEM_ID,
            "BW_SESSION": "vault_session",
            "BW_CLIENTSECRET": "vault_client_secret",
            "BWS_ACCESS_TOKEN": "machine_token",
            "BITWARDENCLI_APPDATA_DIR": "/private/vault",
            "COMPOSE_PROFILES": "unexpected",
            "PATH": "/bin",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            fallback_env = common.compose_environment(self.config)
            selected_env = common.compose_environment(
                self.config, admin_password=VAULT_PASSWORD
            )
        self.assertEqual(fallback_env, {"PATH": "/bin"})
        self.assertEqual(
            selected_env, {"PATH": "/bin", "ADMIN_PASSWORD": VAULT_PASSWORD}
        )

    def test_lookup_is_only_used_for_up(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
            mock.patch.object(
                common, "bitwarden_password", return_value=VAULT_PASSWORD
            ) as lookup,
            mock.patch.object(common.subprocess, "run") as run,
        ):
            common.run_compose(self.config, "ps", "--all")
            lookup.assert_not_called()
            self.assertNotIn("ADMIN_PASSWORD", run.call_args.kwargs["env"])
            common.run_compose(self.config, "up", "--detach")
            lookup.assert_called_once_with(self.config)
            self.assertEqual(
                run.call_args.kwargs["env"]["ADMIN_PASSWORD"], VAULT_PASSWORD
            )
            common.run_compose(self.config, "down")
            self.assertEqual(lookup.call_count, 1)
            self.assertNotIn("ADMIN_PASSWORD", run.call_args.kwargs["env"])


class SyntheticStartupTests(unittest.TestCase):
    def test_vault_and_fallback_paths_through_real_subprocesses(self) -> None:
        for mode in ("unlocked", "locked"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bin_dir = root / "bin"
                bin_dir.mkdir()
                dotenv = root / ".env"
                original = f"ADMIN_PASSWORD={FALLBACK}\n"
                dotenv.write_text(original, encoding="utf-8")
                dotenv.chmod(0o600)
                result_file = root / "result.json"
                (bin_dir / "bw").write_text(
                    f"#!{sys.executable}\n"
                    "import os, sys\n"
                    f"assert sys.argv[1:] == ['get', 'password', {ITEM_ID!r}, '--nointeraction']\n"
                    "assert os.environ['BW_SESSION'] == 'synthetic_session'\n"
                    "if os.environ['TEST_MODE'] == 'locked':\n"
                    f"    print({VAULT_PASSWORD!r}, file=sys.stderr)\n"
                    "    sys.exit(1)\n"
                    f"print({VAULT_PASSWORD!r})\n",
                    encoding="utf-8",
                )
                (bin_dir / "docker").write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, pathlib, sys\n"
                    "assert sys.argv[1:3] == ['compose', '--env-file']\n"
                    "fallback = pathlib.Path(sys.argv[3]).read_text().split('=', 1)[1].strip()\n"
                    "password = os.environ.get('ADMIN_PASSWORD', fallback)\n"
                    "result = {\n"
                    f"    'using_vault': password == {VAULT_PASSWORD!r},\n"
                    f"    'using_fallback': password == {FALLBACK!r},\n"
                    "    'session_leaked': 'BW_SESSION' in os.environ,\n"
                    "    'item_leaked': 'AI_OBSERVATORY_BITWARDEN_ITEM' in os.environ,\n"
                    "    'password_in_args': password in sys.argv,\n"
                    "}\n"
                    "pathlib.Path(os.environ['TEST_RESULT']).write_text(json.dumps(result))\n",
                    encoding="utf-8",
                )
                (bin_dir / "bw").chmod(0o700)
                (bin_dir / "docker").chmod(0o700)
                config = {
                    "ADMIN_PASSWORD": FALLBACK,
                    "COMPOSE_PROJECT_NAME": "aiobs-123456abcdef",
                }
                env = {
                    "PATH": str(bin_dir),
                    "AI_OBSERVATORY_BITWARDEN_ITEM": ITEM_ID,
                    "BW_SESSION": "synthetic_session",
                    "TEST_MODE": mode,
                    "TEST_RESULT": str(result_file),
                }
                warning = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch.object(common, "ROOT", root),
                    mock.patch.object(common, "CONFIG", dotenv),
                    contextlib.redirect_stderr(warning),
                ):
                    common.run_compose(config, "up", "--detach")
                self.assertEqual(
                    json.loads(result_file.read_text(encoding="utf-8")),
                    {
                        "using_vault": mode == "unlocked",
                        "using_fallback": mode == "locked",
                        "session_leaked": False,
                        "item_leaked": False,
                        "password_in_args": False,
                    },
                )
                self.assertEqual(dotenv.read_text(encoding="utf-8"), original)
                self.assertNotIn(VAULT_PASSWORD, warning.getvalue())
                self.assertNotIn(FALLBACK, warning.getvalue())
                if mode == "locked":
                    self.assertIn("using .env fallback", warning.getvalue())


@unittest.skipUnless(shutil.which("docker"), "Docker CLI not installed")
class RealComposeTests(unittest.TestCase):
    def test_real_compose_interpolates_vault_and_dotenv_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            with (
                mock.patch.object(initialize, "CONFIG", dotenv),
                mock.patch.object(common, "CONFIG", dotenv),
                mock.patch.dict(
                    os.environ,
                    {
                        "AI_OBSERVATORY_ADMIN_EMAIL": "test@example.invalid",
                        "AI_OBSERVATORY_ADMIN_PASSWORD": FALLBACK,
                    },
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                initialize.initialize()
                config = common.load_config(dotenv)
                for override in (None, VAULT_PASSWORD):
                    with self.subTest(using_vault=override is not None):
                        rendered = subprocess.run(
                            common.compose_command(
                                config, "config", "--format", "json"
                            ),
                            env=common.compose_environment(
                                config, admin_password=override
                            ),
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=30,
                        )
                        environment = json.loads(rendered.stdout)["services"]["web"][
                            "environment"
                        ]
                        self.assertEqual(
                            environment["LANGFUSE_INIT_USER_PASSWORD"],
                            override or FALLBACK,
                        )
                self.assertEqual(common.load_config(dotenv)["ADMIN_PASSWORD"], FALLBACK)


if __name__ == "__main__":
    unittest.main()
